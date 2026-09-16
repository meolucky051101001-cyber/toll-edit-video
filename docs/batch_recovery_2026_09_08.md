# Cập nhật batch và giám sát — 2026-09-08

Mã nguồn đang sửa ở C:\tool v1\backend; gói ZIP/export cũ là snapshot trước sửa.

## Đã triển khai

- Khóa sở hữu batch có guard liên tiến trình; API đặt chỗ trước khi trả 202.
- Nhận diện owner đã thoát: status interrupted, active=false; người dùng bấm chạy lại.
- Dọn khóa khi callback lỗi, hàng đợi rỗng hoặc tác vụ kết thúc; giữ khóa trong lúc chờ thread hoàn tất.
- Stop theo job_id lưu riêng để không bị cập nhật tiến độ ghi đè.
- FFmpeg trích âm, render và Demucs kiểm tra Stop trong khi chạy.
- Whisper và dịch có checkpoint JSON kiểm tra SHA-256 đầu vào, mã pipeline và output.
- FFprobe kiểm tra thành phẩm có video, thời lượng và audio trước khi xuất.
- Receipt SHA-256 xác nhận input/output trước khi bỏ qua video trùng.
- Thành phẩm cũ thiếu receipt được giữ nguyên và báo lỗi cần kiểm tra; không tự ghi đè.
- Archive file gốc tránh ghi đè tên trùng. Cleanup yêu cầu đường dẫn trong workspace và marker sở hữu.
- A2UI generate dùng cùng kiểm tra đường dẫn video với stream.

## API thực tế và giới hạn

GET /api/status dùng active, status, video_name, step, percent, queue_index,
queue_total, job_id, elapsed_seconds, eta_seconds. GET /api/phoi và /api/banve
dùng path, files, total_count, total_size_mb. Run trả 202 hoặc 409.

Restart không tự chạy lại pipeline hoặc tự tải model. Người dùng bấm Run để chạy lại;
checkpoint hợp lệ sẽ bỏ qua nhận dạng/dịch. Các bước khác hiện chạy lại.
Checkpoint chưa bao phủ OCR/TTS/render. Cấu hình môi trường/model thay đổi cần
xóa checkpoint tương ứng trước khi chạy lại.

Whisper/OCR/TTS đang thực thi trong bộ nhớ chờ điểm dừng an toàn; chưa hỗ trợ
ép dừng toàn bộ model worker. Khóa này bảo vệ batch entrypoint, chưa hợp nhất
mọi API xử lý đơn lẻ và mọi đường chạy Telegram thành một scheduler.

A2UI action vẫn là demo RPC, chưa áp dụng voice/subtitle/mixer vào render.
Dashboard vẫn polling. Upload, chọn giọng và SSE/WebSocket chưa triển khai.

## Kiểm thử

148 test PASS. Bộ test được chạy lại sau thay đổi cuối.
Lệnh: backend\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
Có test subprocess thật cho Stop, checkpoint hỏng/thay input, callback lỗi,
batch rỗng, owner chết, stop không bị cập nhật tiến độ ghi đè.
Chưa chạy video AI đầu-cuối thực tế hoặc restart server đang phục vụ.
