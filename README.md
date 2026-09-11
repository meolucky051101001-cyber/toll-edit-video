# AutoDub Video Bot (Tool V1.0 Final)

Công cụ tự động tải video, tách âm thanh (Demucs), nhận dạng giọng nói (Faster-Whisper Large-v3 Turbo), dịch phụ đề (Gemini AI), lồng tiếng bằng AI Clone Giọng (RVC/Edge/CapCut) và tự động nhận diện khớp vị trí phụ đề gốc (PP-OCRv6 Tiny/EasyOCR).

## Tính năng chính
- **Tải video đa nền tảng**: TikTok, Douyin, Xiaohongshu, YouTube, Facebook, Instagram.
- **Tách Vocal sạch**: Dùng **Demucs (htdemucs)** để ưu tiên tốc độ cho video đơn giản và giữ nhạc nền phục vụ bước mix.
- **Nhận diện giọng nói**: Faster-Whisper Large-v3 Turbo ưu tiên tốc độ, tự fallback Large-v3 khi cần.
- **Dịch thông minh với Gemini**: Dịch ngữ cảnh sát nghĩa, tự động bám theo nội dung video. Có fallback sang mô hình nhỏ khi lỗi (Gemini 1.5 Flash/HuggingFace/FPT).
- **Lồng tiếng RVC Voice Clone**: Chuyển giọng TTS (Edge/Capcut) sang giọng nói cá nhân đã qua huấn luyện.
- **Robust Y-Tracking**: PP-OCRv6 Tiny chạy cô lập bằng ONNX Runtime, tự fallback EasyOCR để không làm hỏng job.
- **Quản lý an toàn (V1 Final)**: Dashboard FastAPI, Telegram Bot độc lập, Lock history đồng bộ, Atomic file tracking, và tự động thu hồi tiến trình lỗi.

## Cấu trúc thư mục
- ackend/: Mã nguồn Python xử lý AI, OCR, RVC và API.
- setup_v1.ps1: Script cài đặt 1-click tự động chuẩn bị môi trường trắng.
- start_bot.bat: Script khởi động nhanh hệ thống trên Windows.

## Hướng dẫn cài đặt (Môi trường trắng)
1. **Yêu cầu**: Đã cài đặt Python 3.10+ và FFmpeg (cần thêm vào PATH).
2. Chạy setup_v1.ps1 bằng PowerShell để tự tạo env, cài thư viện equirements.txt và thư mục runtime.
3. Điền các khóa API vào file ackend/.env (sinh ra từ .env.example).
4. Khởi chạy bằng start_bot.bat. Dashboard quản lý mặc định mở ở port 8088.

## Release Note - Tool V1.0 Final
Đây là phiên bản ổn định (Stable/Final) hoàn tất chu trình MASTER FIX PLAN (Phase 0 -> Phase 8).
- **Khắc phục lỗi ngốn GPU**: Quản lý bộ nhớ nghiêm ngặt qua mô hình Process-Isolation cho OCR và Whisper.
- **Resilience**: Tự động thử lại (Retry) với các luồng dịch lỗi, chống treo tiến trình, xử lý mất mạng khi tải video.
- **Reproducible**: Đóng gói hoàn chỉnh, không rác code, loại bỏ dependency phức tạp (Frontend Electron cũ).
