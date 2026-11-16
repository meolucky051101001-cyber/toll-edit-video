# Sửa phụ đề SRT bệt đen — 02/11/2026

Phụ đề SRT giữ màu chữ người dùng chọn, bổ sung viền đen/trắng tương phản theo
độ sáng của màu chữ. Viền giảm từ 2 xuống 0.7, bóng từ 1 xuống 0.3 tại cỡ chữ
14 để không lấp nét và dấu. Chấp nhận màu ASS sáu/tám ký tự hex và #RRGGBB;
cấu hình màu sai dùng chữ đen viền trắng. Loại ký tự phân cách nguy hiểm trong
tên font. Không ép style SRT lên ASS. Đo thời gian vẫn gắn vào process_video.

## Nghiệm thu

Script scripts/verify_srt_render.py tạo thư mục đầu ra mới, không ghi đè dữ
liệu nghiệm thu. Dùng FFmpeg/NVENC thật, không cần API mạng.

Các ca: ngang/dọc với nền sáng/tối, đen trên đen, trắng trên trắng, hai dòng
tiếng Việt và ASS. Kiểm tra pixel phụ đề xuất hiện/biến mất đúng thời gian,
vùng chữ nằm trong khung, mức độ bệt, luồng video/audio, thời lượng và giải mã
toàn file. Đây là kiểm tra tổng hợp và xem ảnh, không phải OCR chấm mọi câu chữ.

Kiểu cũ được tái hiện qua --baseline-style và bị từ chối (density=0.72927).
Bản sửa đạt tám ca; đã xem trực tiếp ảnh chữ sáng/tối, hai dòng và ASS.
Full suite Python 3.12 bản cuối: 1290 passed, 80 skipped, 0 failed (112.55s).
Nhóm renderer/OCR: 55/55 pass trên cả Python 3.12 và 3.10; JavaScript 13/13.
Ngưỡng mật độ nét được kiểm chứng trên fixture/font của máy này; chưa kết luận
áp dụng cho mọi font hoặc thay cho nghiệm thu video công việc thực tế.

```powershell
& 'C:\tool v1\backend\venv\Scripts\python.exe' -B scripts/verify_srt_render.py --output scratch/new-srt-acceptance
```

Lệnh yêu cầu thư mục đầu ra chưa tồn tại. Log và ảnh/video cuối được giữ tại
C:\tool_v1_clean\queue-schedule-fix\scratch\srt-contrast-final.

## Phạm vi còn lại

80 test skip của repo chưa được gỡ trong bản sửa này. Video chuyển cảnh/nền
phức tạp, câu rất dài, toàn chuỗi AI cold/warm/resume và chạy nhiều job vẫn cần
nghiệm thu riêng. Không dùng kết quả tám video tổng hợp để tuyên bố đã hoàn tất
các phần này.

## Triển khai và hậu kiểm

Đã triển khai 5 file tại C:\tool v1 sau kiểm tra mã nguồn và idle/queue rỗng.
Scoped shutdown dừng 14 tiến trình V1 đã xác minh, không force; khôi phục 5
Scheduled Task đã kiểm chứng. Hai file có sẵn được sao lưu tại:
C:\tool_v1_clean\queue-schedule-fix\scratch\before-srt-contrast-99fd949afcb84842a47d871a7da955ba.

5/5 file triển khai khớp worktree đã test. Chạy lại tám ca bằng script ở bản
production: 8/8 đạt; bằng chứng trong scratch/srt-production-acceptance.
Health 5/5, preflight 25/25, pip check sạch; Telegram running, heartbeat/polling
true, busy=false, queue=0. Downloader PID 7568 giữ thời điểm 17:06:46 ngày
02/11/2026. Không commit/push, không cài dependencies và không dùng API trả phí.
