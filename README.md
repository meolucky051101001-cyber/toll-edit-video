# AutoDub Video Bot (Python + AI)

## Phiên bản trên GitHub

- **Tool V1:** [`tool-v1`](https://github.com/meobeo240107/auto-dubbing-bot/tree/tool-v1)
- **Tool V2 production:** [`tool-v2`](https://github.com/meobeo240107/auto-dubbing-bot/tree/tool-v2)

Hai phiên bản được giữ ở hai nhánh độc lập. Không sao chép `.env`, token, video
người dùng, virtualenv hoặc model cache vào Git. Xem phạm vi bàn giao tại
[`VERSIONS.md`](VERSIONS.md).

Công cụ tự động tải video, tách giọng bằng BS-RoFormer, nhận dạng/căn lời bằng
Qwen3-ASR + ForcedAligner, đọc phụ đề hình bằng PP-OCRv6, dịch với Gemini,
lồng tiếng RVC và render/QC bằng FFmpeg.

## 🚀 Tính năng chính
- **Tải video đa nền tảng**: TikTok, Douyin, Xiaohongshu, YouTube, Facebook, Instagram.
- **Tách Vocal sạch**: **BS-RoFormer**, fallback Demucs `htdemucs_ft`.
- **Nhận diện giọng nói**: **Qwen3-ASR 0.6B + ForcedAligner 0.6B**, fallback Whisper Large-v3.
- **Dịch thông minh với Gemini**: Dịch ngữ cảnh sát nghĩa, tự động bám theo nội dung video.
- **Lồng tiếng RVC Voice Clone**: Chuyển giọng TTS sang giọng nói cá nhân đã qua huấn luyện.
- **OCR phụ đề**: **PP-OCRv6**, fallback EasyOCR; phụ đề Việt bám đúng cửa sổ lời thoại.
- **Bot Telegram**: Điều khiển và nhận video trực tiếp qua ứng dụng Telegram.

## 🛠️ Cấu trúc thư mục
- `backend/`: Mã nguồn Python xử lý AI, OCR, RVC và Telegram Bot.
- `backend/ai/source_separation.py`: Chính sách BS-RoFormer và Demucs fallback.
- `backend/pipeline_v2/content.py`: Hàm thuần cho SRT, OCR geometry và tìm model RVC.
- `backend/telegram_jobs.py`: Chuẩn hóa đường dẫn và thông báo job Telegram V2.
- `frontend/`: Giao diện ứng dụng (Desktop / Electron / Vite UI).
- `start_bot.bat`: Script khởi động Bot nhanh trên Windows.

## 📌 Hướng dẫn chạy Tool
1. Tạo môi trường virtualenv và cài đặt dependencies:
   ```bash
   cd backend
   python -m venv venv
   .\venv\Scripts\activate
   cd ..
   # Cài PyTorch + TorchAudio CUDA phù hợp driver NVIDIA trước.
   # Chọn lệnh Windows chính xác tại https://pytorch.org/get-started/locally/
   pip install -r requirements.txt
   cd backend
   Copy-Item .env.example .env
   # Điền BOT_TOKEN/GEMINI_API_KEY và các thư mục AUTODUB_* trong .env.
   ```
2. Chạy Telegram Bot:
   ```bash
   # Kiểm tra dependency, FFmpeg/NVENC, CUDA, thư mục ghi và secrets trước.
   .\venv\Scripts\python.exe -m pipeline_v2.preflight --project-root .. --interface telegram
   python telegram_bot.py
   ```

Không chạy video production nếu preflight còn `error`; với Pipeline v2, kiểm tra
thêm `pipeline:config` đang là `v2`. Unit test không thay thế lượt thử end-to-end
với model, API key và driver thật của máy vận hành.

## Pipeline v2 production

Nhánh `tool-v2` mặc định chạy Pipeline v2 với checkpoint theo stage, process
GPU riêng, timing solver, FFmpeg ducking/loudness và QC gate ở chế độ `block`.
Xem feature flag và rollback tại
[`docs/pipeline_v2_rollout.md`](docs/pipeline_v2_rollout.md).

Pipeline v2 giới hạn tài nguyên theo batch thay vì theo độ dài video. Vì vậy cùng
một luồng xử lý được dùng cho video ngắn và video dài: dịch/OCR/TTS/RVC có batch
checkpoint, mixer tạo voice bus phân tầng để không vượt giới hạn dòng lệnh
Windows, còn FFmpeg/Demucs/Whisper tiếp tục xử lý streaming hoặc theo segment.
