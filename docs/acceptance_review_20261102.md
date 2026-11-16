# Nghiệm thu độc lập Tool V1 — 02/11/2026

Kết luận: bản nâng cấp đạt kiểm tra hồi quy và vận hành; chưa đạt nghiệm thu
chất lượng video đầy đủ vì phát hiện lỗi hiển thị phụ đề SRT mặc định.

## Bằng chứng mới

- 22/22 file thay đổi đang triển khai khớp worktree đã kiểm thử.
- Chạy lại nhóm dashboard, Gemini/cache, OCR, SenseVoice, giọng nói, runtime,
  queue và schedule trên Python 3.12: 94 passed, 7 skipped, 0 failed (18.23s).
- JavaScript: 13/13 passed. git diff --check không có lỗi whitespace.
- Health 5/5, preflight 25/25, Python 3.12.14, dependencies không xung đột.
- Telegram running, heartbeat/polling true, busy false, queue 0; dashboard idle.
- Hai trang Studio có token không rỗng, giống nhau, không còn placeholder.
- Downloader PID 7568 vẫn có thời điểm khởi động 02/11/2026 17:06:46.
- Log full suite từ lượt triển khai: Python 3.12 và 3.10 đều 1284 passed,
  80 skipped, 0 failed. Không chạy lại full suite trong lượt nghiệm thu này.

## P2 — Phụ đề SRT mặc định bệt đen, khó đọc

Vị trí: backend/video_utils.py:513 và :570. process_video mặc định dùng chữ
đen (&H00000000), Outline=2 nhưng không đặt OutlineColour tương phản. Với
video thử nền trắng và SRT tiếng Việt, phần chữ xuất ra thành một dải đen.
Lỗi thuộc đường render SRT; chưa kết luận ảnh hưởng đường ASS.

Đã trích khung hình tại 1,5 giây của video thử trước đó, rồi render đối chứng
bằng cùng hàm, cùng nguồn, SRT và âm thanh, chỉ truyền màu chữ trắng. Bản đối
chứng đọc rõ câu "Kiểm tra phụ đề tiếng Việt". Cả hai lần đều xuất file thành
công, cho thấy chỉ kiểm tra codec/thời lượng/giải mã chưa đủ nghiệm thu phụ đề.

Ảnh đối chiếu trong worktree:

- scratch/upgrade-render/acceptance-black-crop.png
- scratch/upgrade-render/acceptance-white-crop.png

video_utils.py không khác HEAD: đây là lỗi tồn tại trong đường render được
nghiệm thu, không phải thay đổi mới do bản sửa token/OCR vừa triển khai.

Hướng xử lý: quy định màu viền tương phản với màu chữ SRT, giữ lựa chọn màu
chữ của người dùng; kiểm tra hình ảnh trên nền sáng/tối và chữ có dấu. Sau sửa,
render lại cả SRT và ASS để chứng minh không gây hồi quy.

## Chưa được nghiệm thu

- 80 test đang skip vẫn chưa chứng minh chức năng tương ứng hoạt động đúng.
- Chưa có nghiệm thu toàn chuỗi AI bằng video công việc thật, cold/warm/resume,
  chất lượng dịch/TTS thực tế hoặc độ ổn định qua nhiều job.

Lượt này không sửa mã sản phẩm, không restart dịch vụ và không dùng API trả phí.
Chỉ tạo ảnh/video đối chứng mới trong scratch và tài liệu nghiệm thu này.
