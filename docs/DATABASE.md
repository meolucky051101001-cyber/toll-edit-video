# Database

SQLAlchemy 2 declarative models, SQLite WAL + foreign_keys. Path tương đối resolve theo root dự án, không theo thư mục người dùng mở command.

| Bảng | Vai trò |
|---|---|
| videos | Metadata chuẩn hóa, trạng thái người dùng, canonical URL/fingerprint, điểm lần đầu |
| search_jobs | Query, nền tảng, tiến độ, lỗi, timestamps và nhãn mock |
| search_queries | Query thực sự đã dùng cho từng provider trong job |
| search_plans | Một plan/job: use_ai, queries JSON, expansion JSON, source, warning |
| job_videos | Quan hệ nhiều-nhiều, bộ điểm riêng từng job |

Unique `(platform, platform_video_id)` và `canonical_url`. URL bỏ tracking params đã biết nhưng giữ tham số định danh như `v`. Fallback fingerprint chỉ dùng khi thiếu video ID và có đủ caption + author/thumbnail.

Khi video tìm lại: không thêm row; cập nhật các engagement không NULL, không ghi đè trạng thái save/skip. Quan hệ job_videos giữ được kết quả của các lần tìm cũ. Điểm khi GET videos?job_id lấy từ link của job đó; Library dùng điểm đầu tiên của video.

Commit mỗi batch 8 kết quả; không giữ transaction qua await. SQLite một worker, tránh writer tranh chấp. Job restart không tự chạy tiếp: đánh dấu failed và cho người dùng chạy lại.

Phase 2 bổ sung bảng search_plans bằng create_all, không sửa hoặc xóa bảng/cột hiện có. Job cũ thiếu plan vẫn đọc được. Thay đổi cột về sau cần migration riêng; create_all không tự migrate cột. PostgreSQL cần driver, migration và integration test; chưa tuyên bố hỗ trợ production.


Import bridge bổ sung bảng `import_batches` (nguồn, SHA-256, tổng/bỏ qua, FK job) và `import_origins` (nguồn đầu tiên của video). `create_all` chỉ thêm hai bảng, không sửa cột. Commit lượt nhập là một transaction; mỗi lượt có job_videos riêng, record trùng giữ metadata/trạng thái hiện có.
