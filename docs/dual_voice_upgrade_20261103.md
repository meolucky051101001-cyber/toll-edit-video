# Dual voice cho hội thoại luân phiên — 03/11/2026

Phạm vi người dùng chọn: nam/nữ nói luân phiên nhưng bị gán nhầm giọng.
Không có video lỗi thực tế vì video đã xóa.

## Thay đổi

- Bỏ suy đoán giới tính câu unknown bằng câu đứng trước/hai câu bên cạnh.
  Câu trả lời ngắn của người còn lại không bị ghi nhận thành người đang nói
  chỉ vì vị trí thời gian; giữ nguyên lý do thiếu bằng chứng/xung đột âm thanh.
- Câu thiếu index dùng đúng vị trí 1..N trong cả phân tích và ánh xạ giọng.
  Trước đây bước gán giọng dùng index=1 cho mọi câu thiếu index, làm ghi đè map.
- Từ chối ID trùng trước khi đọc cache hoặc gán giọng.
- Snapshot mới lưu cue_hash của ID, mốc đầu/cuối và nội dung. Khi các giá trị
  này đổi, phân tích lại thay vì dùng bản phân vai cũ của cùng job.
- Snapshot mới ghi analysis_version=dual_voice_evidence_2 và requires_voice_review.
  Job legacy đã khóa có job_id khớp vẫn giữ giọng khi resume; chọn lại rõ ràng
  bằng force_reselect hoặc job mới mới áp dụng chính sách mới cho job đó.

## Giới hạn cần hiểu đúng

Đây là nâng cấp tính đúng của việc gán giọng/cache, chưa thay mô hình phân biệt
người nói. Bộ phân tích vẫn dựa trên cao độ và độ rõ của âm thanh, không thể
đảm bảo giọng nam cao/giọng nữ trầm luôn được gán đúng. Không tách hai người
nói chồng nhau. Câu unknown được đánh dấu review trong snapshot và vẫn dùng
giọng dự phòng đã cấu hình khi TTS; chưa có màn hình chỉnh vai từng câu mới.

## Bằng chứng

- Baseline 16 đoạn từ bốn mẫu giọng hiện có: 15 nhận đúng, một unknown.
  Không dùng tập nhỏ này để tuyên bố phần trăm chính xác trên video thực tế.
- Ghép bốn mẫu giọng thành tám lượt nam/nữ luân phiên: 8/8 ánh xạ đúng cả
  gender và voice_id (BV075/BV562). Audio và báo cáo: scratch/dual-turn-audio.
- Hồi quy trọng tâm Python 3.12: 34 passed, 2 skipped.
- Kiểm thử mới và cache job trên Python 3.10: 13 passed.
- Full suite Python 3.12: 1300 passed, 80 skipped, 0 failed (115.13s).
- Kiểm thử bao gồm câu ngắn unknown, nguồn âm thanh bất đồng, cue mixed,
  thiếu/trùng ID, sửa mốc/nội dung phụ đề và resume không đổi.

Không cài model/dependencies mới, không gửi tin Telegram hoặc gọi API trả phí.

## Triển khai và hậu kiểm

Đã nạp năm file vào C:\tool v1 sau kiểm tra idle và queue rỗng. Scoped shutdown
dừng 16 tiến trình V1 đã xác minh, không force, sau đó khôi phục năm Scheduled
Task. Backup mã trước sửa ở:
C:\tool_v1_clean\queue-schedule-fix\scratch\before-dual-voice-34c69b9b7c864591a132fe9246c719d3.

Hậu kiểm từng bị gián đoạn do hệ thống duyệt tự động hết hạn mức; sau yêu cầu
tiếp tục của người dùng, đã thực hiện thành công cùng đường kiểm tra được duyệt.
Mã production phân vai đúng 8/8 lượt mẫu; bằng chứng ở scratch/dual-turn-production.
Năm file triển khai khớp worktree. Health 5/5, preflight 25/25, pip check sạch;
dashboard idle, Telegram running/heartbeat/polling=true, busy=false, queue=0.
Downloader PID 7568 vẫn có thời điểm khởi động 02/11/2026 17:06:46.
Chưa commit hoặc push; giữ các thay đổi chưa commit hiện có.
