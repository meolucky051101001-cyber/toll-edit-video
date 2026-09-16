# AutoDub Tool V1 — Bàn giao

Đây là cây mã nguồn riêng của nhánh `tool-v1`. Tool V1 dùng cho video đơn giản,
ưu tiên xử lý nhanh và ít VRAM; không dùng hoặc thay đổi cấu hình Pipeline V2.

## Model mặc định

- Tách giọng: Demucs `htdemucs` Fast.
- Nhận dạng: Faster-Whisper `large-v3-turbo`, fallback `large-v3`.
- OCR: `PP-OCRv6_tiny_det` + `PP-OCRv6_tiny_rec` qua ONNX Runtime,
  fallback EasyOCR CPU.
- Dịch: Gemini 3.8 Flash và các provider fallback hiện có.
- Lồng tiếng: dùng model RVC hiện có khi runtime `rvc-python` tương thích; nếu
  không thì tự chuyển sang Edge TTS để job vẫn hoàn thành.

## Cài đặt

1. Chạy `setup_v1.ps1 -PreloadOCR` để tạo môi trường và tải trước model OCR.
2. Sao chép `backend/.env.example` thành `backend/.env`, giữ
   `PIPELINE_MODE=legacy`, rồi điền token/API key riêng của bot V1.
3. Chạy preflight và xử lý mọi mục `error`:

   ```powershell
   .\backend\venv\Scripts\python.exe .\backend\v1_preflight.py --project-root . --interface telegram
   ```

4. Chạy `start_bot.bat` cho Telegram hoặc `run_batch_edit.bat` cho thư mục máy.

Thành phẩm mặc định được lưu vào `D:\banve`. Script khởi động V1 không dừng
bất kỳ tiến trình Telegram nào khác, nên không ảnh hưởng bot Tool V2.
