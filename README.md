# AutoDub Video Bot (Tool V1.0 Final)

Công cụ tự động tải video, tách âm thanh (Demucs), nhận dạng giọng nói (Faster-Whisper Large-v3 Turbo), dịch phụ đề (Gemini AI), lồng tiếng bằng AI Clone Giọng (RVC/Edge/CapCut) và tự động nhận diện khớp vị trí phụ đề gốc (PP-OCRv6 Tiny/EasyOCR).

## Tính năng chính
- **Tải video đa nền tảng**: TikTok, Douyin, Xiaohongshu, YouTube, Facebook, Instagram với finite timeout và atomic download.
- **Tách Vocal sạch**: Dùng **Demucs (htdemucs)** để ưu tiên tốc độ cho video đơn giản và giữ nhạc nền phục vụ bước mix.
- **Nhận diện giọng nói**: Faster-Whisper Large-v3 Turbo chạy qua tiến trình con độc lập (`v1_asr_isolated.py`), không lưu rác RAM/VRAM.
- **Dịch thông minh với Gemini**: Dịch ngữ cảnh sát nghĩa, CJK fail-closed guard, fallback theo chuỗi an toàn.
- **Lồng tiếng RVC Voice Clone**: Hỗ trợ RVC, Edge-TTS, CapCut, FPT API; toàn bộ tác vụ voice chạy qua subprocess worker (`v1_voice_isolated.py`).
- **Robust Y-Tracking**: PP-OCRv6 Tiny chạy cô lập, tự fallback EasyOCR để không làm hỏng job.
- **Bảo vệ toàn diện**:
  + Chống DNS Rebinding: Kiểm tra chặt chẽ `Host` header (chỉ cho phép localhost, 127.0.0.1, [::1]).
  + Unified Pipeline Lock: Tuần tự hóa tuyệt đối toàn bộ GPU lifecycle giữa Batch Runner, Subtitle Generation và Render.
  + Authenticated Control: Dừng tiến trình nhẹ nhàng (Graceful Stop) qua runtime token bảo mật.

## Cấu trúc thư mục
- `backend/`: Mã nguồn Python xử lý AI, OCR, RVC, Dashboard FastAPI và Telegram Bot.
- `setup_v1.ps1`: Script cài đặt 1-click tự động chuẩn bị môi trường trắng.
- `start_bot.bat`: Khởi động Dashboard (cổng 8088) và Telegram Bot an toàn.
- `stop_bot.bat`: Dừng an toàn tiến trình bot và worker của Tool V1 (không ảnh hưởng Tool V2).
- `Giao_Dien_Quan_Sat_Tool.bat`: Bảng điều khiển quản sát và chuyển đổi V1/V2 (cổng 8090).

## Hướng dẫn cài đặt (Môi trường trắng)
1. **Yêu cầu**: Đã cài đặt Python 3.10+ và FFmpeg (cần thêm vào PATH).
2. Chạy `setup_v1.ps1` bằng PowerShell để tự tạo venv, cài thư viện từ `requirements.txt` và khởi tạo thư mục runtime.
3. Điền các khóa API vào file `backend/.env` (sinh ra từ `.env.example`).
4. Khởi chạy bằng `start_bot.bat`. Dashboard quản lý mặc định mở ở port 8088.

## Quản lý & Vận hành
- **Dashboard Web**: Truy cập `http://127.0.0.1:8088` để chạy render đơn lẻ, tải video hoặc xử lý thư mục batch.
- **Bộ điều khiển trung tâm**: Chạy `Giao_Dien_Quan_Sat_Tool.bat` để truy cập bộ chuyển đổi V1 / V2 tại `http://127.0.0.1:8090`.
- **Dừng bot**: Chạy `stop_bot.bat` khi muốn dừng bot độc lập mà không ảnh hưởng tới tiến trình khác.
