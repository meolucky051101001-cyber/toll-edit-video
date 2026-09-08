# AutoDub Video Bot (Python + AI)

Công cụ tự động tải video, tách âm thanh nhân vật (Demucs), nhận dạng giọng nói (Faster-Whisper Large-v3 Turbo), dịch phụ đề (Gemini AI), lồng tiếng bằng AI Clone Giọng (RVC) và tự động nhận diện khớp vị trí phụ đề gốc (PP-OCRv6 Tiny/EasyOCR).

## 🚀 Tính năng chính
- **Tải video đa nền tảng**: TikTok, Douyin, Xiaohongshu, YouTube, Facebook, Instagram.
- **Tách Vocal sạch**: Dùng **Demucs (`htdemucs`)** để ưu tiên tốc độ cho video đơn giản và giữ nhạc nền phục vụ bước mix.
- **Nhận diện giọng nói**: Faster-Whisper Large-v3 Turbo ưu tiên tốc độ, tự fallback Large-v3 khi cần.
- **Dịch thông minh với Gemini**: Dịch ngữ cảnh sát nghĩa, tự động bám theo nội dung video.
- **Lồng tiếng RVC Voice Clone**: Chuyển giọng TTS sang giọng nói cá nhân đã qua huấn luyện.
- **Robust Y-Tracking**: PP-OCRv6 Tiny chạy cô lập bằng ONNX Runtime, tự fallback EasyOCR để không làm hỏng job.
- **Bot Telegram**: Điều khiển và nhận video trực tiếp qua ứng dụng Telegram.

## 🛠️ Cấu trúc thư mục
- `backend/`: Mã nguồn Python xử lý AI, OCR, RVC và Telegram Bot.
- `frontend/`: Giao diện ứng dụng (Desktop / Electron / Vite UI).
- `start_bot.bat`: Script khởi động Bot nhanh trên Windows.

## 📌 Hướng dẫn chạy Tool

1. Cài môi trường chính và model OCR cô lập:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\setup_v1.ps1 -PreloadOCR
   ```

2. Tạo cấu hình riêng cho bot V1:

   ```powershell
   Copy-Item .\backend\.env.example .\backend\.env
   ```

   Giữ `PIPELINE_MODE=legacy`, sau đó điền `BOT_TOKEN` và các API key của V1.
   Không sao chép token của bot V2 sang file này.

3. Kiểm tra trước khi chạy Telegram:

   ```powershell
   .\backend\venv\Scripts\python.exe .\backend\v1_preflight.py --project-root . --interface telegram
   ```

4. Khởi động `start_bot.bat`, hoặc dùng `run_batch_edit.bat` để xử lý video cục bộ.

Không chạy bot Telegram nếu preflight còn `error`. Chế độ batch vẫn có thể sẵn
sàng khi chưa cấu hình token Telegram. Thành phẩm mặc định được ghi vào
`D:\banve`.

## Tách biệt với Tool V2

Các entrypoint vận hành của nhánh này buộc `PIPELINE_MODE=legacy`. Script khởi
động V1 không dừng tiến trình Telegram khác, không dùng cấu hình bot V2 và không
thay đổi cây mã nguồn `C:\tool v2`.
