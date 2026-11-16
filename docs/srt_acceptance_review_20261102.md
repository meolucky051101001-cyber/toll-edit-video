# Nghiệm thu độc lập bản sửa SRT — 02/11/2026 23:54 +07

Kết luận: ĐẠT trong phạm vi sửa phụ đề SRT bệt đen và kiểm tra hồi quy renderer.
Chưa phát hiện lỗi mới cần sửa trong phạm vi này.

## Kiểm chứng mới

- 5/5 file của bản sửa triển khai khớp worktree đã kiểm thử.
- Python 3.12: chạy lại renderer/OCR/quality gate, 55 passed trong 9.54s.
- JavaScript: chạy lại 13/13 passed.
- Chạy script từ C:\tool v1: tám ca render NVENC mới đều đạt. Đã xem trực tiếp
  tám ảnh: nền sáng/tối, ngang/dọc, chữ cùng màu nền, hai dòng tiếng Việt, ASS.
  Chữ/dấu đọc được; hai dòng tách biệt. Kiểm tra thời điểm có/không có chữ,
  vị trí trong khung, luồng video/audio, thời lượng và giải mã đều đạt.
- Ca âm tái hiện kiểu cũ bị từ chối đúng dự kiến: exit 1, passed=false,
  density=0.72927. Các ca bản sửa có density từ 0.30927 đến 0.48840.
- Health 5/5, preflight 25/25, Python 3.12.14, pip check sạch.
- Sau render: dashboard idle/active=false; Telegram running, heartbeat=true,
  polling=true, busy=false, queue=0; không có active_job.
- Downloader PID 7568 giữ thời điểm khởi động 17:06:46 ngày 02/11/2026.
- git diff --check không có lỗi whitespace.

## Bằng chứng và giới hạn

Ảnh/video/report mới ở:
C:\tool_v1_clean\queue-schedule-fix\scratch\srt-independent-acceptance

Ca âm ở:
C:\tool_v1_clean\queue-schedule-fix\scratch\srt-independent-baseline

Đã đối chiếu log full suite trước triển khai: 1290 passed, 80 skipped, 0 failed.
Không chạy lại full suite trong lượt nghiệm thu này. Các skip chưa được gỡ.
Ngưỡng mật độ nét là phép kiểm tra cho fixture/font cụ thể, không thay thế đánh
giá mọi font, nền chuyển động, câu rất dài hoặc chất lượng toàn chuỗi AI.
Nghiệm thu video công việc cold/warm/resume và chạy nhiều job còn mở.

Không sửa mã sản phẩm, không restart dịch vụ, không dùng API trả phí trong
lượt nghiệm thu. Chỉ tạo đầu ra thử mới và tài liệu này.
