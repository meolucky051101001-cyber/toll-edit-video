# AutoDub Video Tools

> [!IMPORTANT]
> **Hệ thống chính thức (Official Production):** Quy trình tự động 8 bước lồng tiếng/dịch thuật AI trên NVIDIA GPU, phục vụ sản xuất thực tế qua Telegram Bot và Dashboard Monitor.

---

## Mục lục

1. [Tổng quan hệ thống](#1-tổng-quan-hệ-thống)
2. [Quy trình xử lý tự động](#2-quy-trình-xử-lý-tự-động)
3. [Model AI & Cấu hình runtime](#3-model-ai--cấu-hình-runtime)
4. [Cài đặt & Khởi động](#4-cài-đặt--khởi-động)
5. [Cổng dịch vụ & Giao diện điều khiển](#5-cổng-dịch-vụ--giao-diện-điều-khiển)
6. [Kiểm thử & Đảm bảo chất lượng](#6-kiểm-thử--đảm-bảo-chất-lượng)
7. [Bảo mật & Quản lý dữ liệu](#7-bảo-mật--quản-lý-dữ-liệu)

---

## 1. Tổng quan hệ thống

| Thành phần | Trạng thái | Cổng truy cập | Vai trò chính |
| --- | --- | --- | --- |
| **Hệ thống lồng tiếng & dịch video**<br>[`tool-v1`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-v1) / [`main`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/main) | **Chính thức** | Dashboard: `http://127.0.0.1:8088`<br>Tool Control: `http://127.0.0.1:8090` | Quy trình tự động 8 bước, Telegram bot, Dashboard real-time, GPU BS-RoFormer, Whisper Turbo & Gemini AI failover. |
| **AI Video Research Tool**<br>[`tool-tim-kiem-video`](https://github.com/meolucky051101001-cyber/toll-edit-video/tree/tool-tim-kiem-video) | Độc lập | Web: `http://localhost:3000`<br>API: `http://localhost:8000` | Nghiên cứu & thu thập video (Douyin/Xiaohongshu); chạy độc lập, không dùng chung quy trình. |

---

## 2. Quy trình xử lý tự động

Quy trình khép kín 8 công đoạn liên hoàn, tối ưu tốc độ và chất lượng:

1. **Tiếp nhận & Chuẩn bị:** Nhận link video (Douyin, TikTok, Xiaohongshu) qua Telegram Bot hoặc file đưa vào hàng đợi; trích xuất audio WAV 44.1kHz.
2. **Tách nguồn âm thanh:** **BS-RoFormer (SDR 12.97 dB)** trên GPU (CUDA 0), môi trường cách ly `backend/model_venv_separator`; tách giọng gốc và nhạc nền (BGM).
3. **Nhận dạng lời nói (ASR):** **Whisper Large-v3 Turbo**, bóc tách lời thoại và timestamp chính xác từng mili-giây.
4. **Nhận diện phụ đề gốc (OCR):** **PP-OCRv6** với Smart Skip và cân bằng hình học phụ đề (OCR Geometry auto-heal).
5. **Dịch thuật AI theo ngữ cảnh:** **Gemini Multi-key Failover Pool** (tự đảo key dự phòng khi chạm quota/rate-limit), văn phong Việt Nam tự nhiên.
6. **Lồng tiếng & Căn nhịp (TTS & Timing):** CapCut TTS đa luồng 6 worker, Vie-TTS / VieNeu 14 giọng Bắc/Trung/Nam, Edge TTS dự phòng; **Timing Solver & Speed Limiter (tối đa 1.45x)** chống méo tiếng, nuốt chữ.
7. **Hòa âm thông minh:** **Hierarchical Soft Ducking** tự hạ nhạc nền khi có lời nói, chuẩn hóa âm lượng **EBU R128 (-15 LUFS)**.
8. **Render tăng tốc phần cứng:** **FFmpeg NVENC H.264**, che phụ đề cũ mượt mà, nhúng phụ đề mới.

---

## 3. Model AI & Cấu hình runtime

Quản lý tập trung qua `backend/v1_model_registry.py` và `backend/ai/v1_model_policy.py`:

| Công đoạn | Model mặc định | Tăng tốc / Runtime | Dự phòng |
| --- | --- | --- | --- |
| Separation | BS-RoFormer Ep317 (SDR 12.97dB) | CUDA:0 (`backend/model_venv_separator`) | Demucs v4 (htdemucs) |
| ASR | Whisper Large-v3 Turbo | CUDA:0 (Faster-Whisper FP16) | Standard Whisper / Fallback Cache |
| OCR | PP-OCRv6 (ONNX Runtime) | ONNX Runtime GPU/DirectML | Smart Skip / Reuse visual bbox |
| Dịch thuật | Gemini Flash (Multi-key Pool) | HTTP REST đa key + cache phiên | Secondary API keys / Tiered fallback |
| TTS | CapCut TTS (6-workers) & Vie-TTS (VieNeu 14 giọng) | Async streaming pooling & fallback tự động | Edge-TTS (4-workers) |
| Render | FFmpeg NVENC H.264 | NVIDIA NVENC Hardware Encoder | libx264 CPU encode |

---

## 4. Cài đặt & Khởi động

### Yêu cầu hệ thống

- Windows 10/11 64-bit.
- GPU NVIDIA (khuyến nghị RTX 3050 trở lên, tối thiểu 4–6GB VRAM, đã cài CUDA Driver).
- Python 3.10; FFmpeg và ffprobe có trong biến môi trường `PATH`.

### Khởi động

Tại thư mục gốc:

| Cách chạy | Thao tác | Dùng khi |
| --- | --- | --- |
| Chạy ngầm toàn bộ hệ thống (khuyến nghị) | Nhấp đúp `KhoiDongAn.vbs` | Dùng hàng ngày — tự khởi động Telegram Bot, Dashboard (8088), Tool Control (8090) |
| Chạy trực tiếp có console | `start_bot.bat` | Debug / xem log |
| Dừng toàn bộ | `stop_bot.bat` hoặc `scripts\windows\TatHoanToan.bat` | Tắt bot, dashboard, tiến trình Python và FFmpeg ngầm |

---

## 5. Cổng dịch vụ & Giao diện điều khiển

- **Dashboard:** `http://127.0.0.1:8088/` — tiến độ 8 bước real-time, hàng đợi, phát video trước/sau, log hệ thống, công tắc nhận dạng giọng.
  - Studio Quy Trình: `http://127.0.0.1:8088/quy-trinh`
  - Studio Kịch Bản: `http://127.0.0.1:8088/kich-ban`
- **Tool Control:** `http://127.0.0.1:8090/` — trạng thái bot, dashboard, heartbeat, hàng đợi xử lý.

---

## 6. Kiểm thử & Đảm bảo chất lượng

```powershell
# Kiểm thử nghiệm thu sản xuất
.\backend\venv\Scripts\python.exe -m pytest tests/test_verify_production_readiness.py tests/test_v1_quality_gate.py
```

- BS-RoFormer: `CUDA:0, SDR 12.97 dB (Ready: True)`.
- Nghiệm thu: 12/12 passed.

---

## 7. Bảo mật & Quản lý dữ liệu

- **Không commit secrets:** `backend/.env` (`BOT_TOKEN`, `GEMINI_API_KEY`, cookie, API keys) tuyệt đối không đẩy lên Git.
- **Dữ liệu runtime** (`workspace/`, video, cache âm thanh, log) đã loại trừ trong `.gitignore`.
- **Model nặng & môi trường ảo** (`backend/venv/`, `backend/model_venv_separator/`) lưu cục bộ, không đẩy lên Git.
