# BÁO CÁO TRẠNG THÁI TOOL V1 (PROJECT STATE & HANDOFF)

*Cập nhật: 28/10/2026. Tài liệu này thay thế bản 03/10/2026 (mô tả Tool V1 + Tool V2 song song).*

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
- Kiểm tra sức khỏe: `GET /api/status`, `GET /api/preflight`

Khởi động / dừng:

- Khởi động toàn bộ: `KhoiDongAn.vbs` (hoặc `start_bot.bat` để thấy console).
- Chỉ mở giao diện quan sát: `Giao_Dien_Quan_Sat_Tool.bat`.
- Dừng an toàn: `stop_bot.bat` hoặc `TatHoanToan.bat`.

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

Điểm chặn lỗi (mặc định mới):

- `V1_QC_POLICY=BLOCK` trong `backend/v1_feature_flags.py` và `backend/main.py`.
- Lỗi toàn vẹn nghiêm trọng **luôn** ném `CriticalQualityError` trước khi giao video, kể cả khi chọn `WARN` hoặc `REPORT_ONLY`.
- Stage 3 và Stage 4 gọi OCR với `strict_geometry=True`; phụ đề phát hiện được nhưng không xác định được vùng che sẽ dừng job (`OCRGeometryError`) thay vì đoán.
- Stage 5 gọi `translate_subtitles(strict=True)`; chỉ cho phép fallback khi đặt `V1_ALLOW_TRANSLATION_FALLBACK=true`.
- Video không có phụ đề gốc vẫn hợp lệ và đi tiếp bình thường.

Khi job dừng ở bước 4/5, hãy sửa nguyên nhân (vùng OCR, ngưỡng, prompt dịch) thay vì hạ chính sách QC. Cờ runtime nằm tại `workspace/bot_system/v1_feature_flags.json` và có thể ghi đè mặc định; file này không được commit. **Lưu ý:** file runtime hiện tại vẫn đặt `V1_QC_POLICY=REPORT_ONLY`; cần đổi sang `BLOCK` trên Dashboard khi muốn chặn giao video lỗi QC (lỗi nghiêm trọng vẫn luôn bị chặn ở mọi chế độ).

---

## 3. MÔI TRƯỜNG RUNTIME

| Môi trường | Mục đích | Ghi chú |
| --- | --- | --- |
| `backend/venv` | Ứng dụng chính (FastAPI, bot, orchestrator) | Python 3.10.11 |
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

## 5. KIỂM THỬ

Bộ kiểm thử an toàn dùng trong CI (`.github/workflows/python-quality.yml`):

```powershell
.\backend\venv\Scripts\python.exe -m pytest -q --tb=short -p no:cacheprovider `
  tests/test_app_paths.py tests/test_ocr_subtitle_locator.py tests/test_url_utils.py `
  tests/test_v1_checkpoint_orchestrator.py tests/test_v1_ocr_geometry_repair.py `
  tests/test_v1_quality_gate.py tests/test_pipeline_v2_failure_semantics.py `
  tests/test_path_hardening.py tests/test_studio_render_safety.py tests/test_stop_flag_hygiene.py
```

- 81 test cũ đã lỗi thời được quarantine trong `tests/conftest.py`; không coi là test hồi quy.
- Trước khi giao thay đổi, chạy tối thiểu nhóm trên và các test liên quan trực tiếp.

---

## 6. THAY ĐỔI GẦN ĐÂY (28/10/2026)

1. Nối fail-closed vào pipeline chính: QC mặc định `BLOCK`, OCR strict ở stage 3/4, dịch strict ở stage 5, giữ nguyên trạng thái `unresolved` khi phát hiện phụ đề nhưng không có band tin cậy.
2. Mở rộng CI sang 10 module kiểm thử an toàn (fail-closed, OCR geometry, render Studio, stop-flag, path hardening).
3. Bổ sung dependency test còn thiếu vào `requirements-dev.txt`; bỏ pin `paddleocr>=2.7,<3` sai khỏi `requirements.txt` để khớp runtime `paddleocr==3.7.0`.
4. Siết bảo mật API local: bắt buộc `X-Local-Control-Token` cho mọi request ghi `/api/*` và `/a2ui/*`, bỏ endpoint `/api/auth-token`; Dashboard/Studio nhận token inject sẵn, Telegram script bot đọc token từ workspace.
5. Xóa mã V2 legacy (`dashboard_monitor.py`, `build_v2_dashboard.py`, test V2) và các nhánh V2 không còn chạy trong control plane.
6. Tối ưu `GET /api/workflow/videos`: chuyển quét/kiểm tra artifact sang thread pool, thêm cache theo mtime/size; thời gian phản hồi giảm còn ~0,4–0,7 giây với ~350 video.
7. Thêm chỉ số RAM vào `/api/status` và thanh RAM trên Dashboard cạnh VRAM.
8. Dọn log/artifact test cũ (~110 MB) và sửa race lost-update khi ghi `.render_history` từ nhiều thread bằng khóa trong process.
9. Cập nhật tài liệu trạng thái này theo thực tế V1-only.

---

## 7. NGUYÊN TẮC VẬN HÀNH AN TOÀN

- Không kill/khởi động lại dashboard, bot hoặc job đang chạy nếu chưa được phép rõ ràng.
- Không đọc/in `.env`, `douyin_cookies.txt`, token hay API key.
- Không tự xóa/ghi đè file stop, queue hoặc dữ liệu thật trong `workspace/`.
- Giữ `PIPELINE_MODE=legacy`; giữ nguyên 8 bước, Telegram bot, `/quy-trinh`, `/kich-ban`.
- FFmpeg crop/blur phải ép `format=yuv420p`.
- Giao diện 100% tiếng Việt, dark mode.
- Visual sub-to-speech là tùy chọn, mặc định tắt (`V1_VISUAL_SUB_TO_SPEECH=false`).
