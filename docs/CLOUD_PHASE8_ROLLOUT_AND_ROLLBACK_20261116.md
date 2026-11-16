# Cloud Tool V1 — Phát hành có kiểm soát và khôi phục

## Điều kiện phát hành

Cloud khởi động ở trạng thái khóa trên DB mới hoặc DB nâng cấp từ Giai đoạn 7. Video Telegram gửi thông thường dùng Local. Dashboard, Telegram và các worker Cloud phải trỏ đến cùng `AUTODUB_WORKSPACE` để dùng chung `cloud_jobs.db`. Chỉ chạy một bản Cloud có quyền ghi lên DB này trong lúc chuyển phiên bản.

Gói phát hành có runner độc lập, manifest môi trường và wheel CapCut đã ghim. Kiểm tra danh mục gói bằng `python package_v1_release.py --dry-run` trên checkout sạch của bản ứng viên.

Kết quả [Canary Giai đoạn 7](CLOUD_PHASE7_ACCEPTANCE_AND_CANARY_20261114.md#10-nghiệm-thu-sau-sửa-toàn-bộ-lỗi-đã-xác-minh--16112026) đạt 6/6 tiêu chí trên ba mẫu đã duyệt, có ca hủy thật và tổng mức giảm số dư 4,20/5 CU. Mức 4,20 CU là quan sát số dư, còn ledger ghi chi phí ước tính runtime. Giữ bản phát hành ở draft cho tới khi kết quả kiểm thử bắt buộc, job thử bản phát hành và biên bản khôi phục đã được xem xét, không còn P0/P1 mở hay tài nguyên Cloud chờ xử lý.

## Chạy thử một job

1. Ghi bản mã đang phục vụ, đường dẫn DB, số dư Colab ban đầu và mã của các session đang chạy. Đối chiếu job, cleanup và ledger hiện có trên Dashboard. Chọn một video đã duyệt, GPU và trần CU cho lượt thử. Kiểm tra hạn mức ngày bao gồm phần CU đã dùng và đang giữ. Lưu biên bản mà không kèm token, cookie, nguồn riêng tư hoặc thông tin truy cập.
2. Trên Dashboard cục bộ, chọn **Mở một lượt Cloud**, rồi chọn **Chạy video này trên Cloud** hoặc gửi `/cloud <video>` qua Telegram. API cục bộ tương ứng là `POST /api/cloud/rollout` với `{"action":"arm"}`, sau đó `POST /api/cloud/enqueue` với `rollout_permit` và `X-Idempotency-Key` của lần gửi. Các POST dùng `X-Local-Control-Token`.
3. Một lượt nhận đúng một job. Ticket và job được lưu trong cùng giao dịch SQLite. Cả worker và luồng Telegram dùng chung giới hạn một job đang chạy trên DB. Nếu gửi lại do lỗi mạng, giữ nguyên khóa idempotency của lần gửi. Các job chờ từ bản cũ không được worker tự lấy.
4. Theo dõi bước, deadline, QC, session và ledger theo mã job. Khi hoàn tất, kiểm tra thành phẩm và manifest QC. Ghi mã job/attempt, mức giảm số dư, chi phí và nguồn đo trong ledger, kết quả thu hồi cùng số dư cuối. Khóa lượt khi kết thúc kiểm tra.

## Theo dõi

`GET /api/cloud/status` trả `rollout`, `status_latency_ms` và `rollout_alerts`. Dashboard hiện cảnh báo bằng mã và mã job. Các bản ghi về QC và gửi lặp được hiển thị trong 24 giờ. Một yêu cầu lặp theo idempotency báo rằng job cũ đã được trả lại. Độ trễ trên 1.000 ms là thời gian xử lý phía dịch vụ trạng thái. Khi quá hạn, kiểm tra job và quyết định hủy theo tình trạng thực tế.

| Mã | Việc cần kiểm tra |
|---|---|
| `CLEANUP_PENDING` | Đối chiếu session thuộc job và xác minh thu hồi. |
| `LEDGER_UNRECONCILED` | Kiểm tra attempt, trạng thái thu hồi và nguồn đo CU. |
| `DEADLINE_EXCEEDED` | Kiểm tra job còn chờ hay chạy và quyết định hủy theo mã job. |
| `QC_FAILED` | Mở báo cáo QC của attempt trước khi cho phép lượt mới. |
| `DUPLICATE_SUBMISSION` | Xác nhận các yêu cầu lặp trả cùng mã job. |
| `STATUS_SLOW` | So thời gian phản hồi với độ mới của cache provider và tải SQLite. |

## Khôi phục

1. Chọn **Khóa nhận job Cloud**, dùng `POST /api/cloud/rollout` với `{"action":"pause"}`, hoặc dùng `/cloud off` trên Telegram. Khóa có hiệu lực cả khi khởi động lại. Ticket cũ hết hiệu lực và job queued cũ sẽ không được claim. Video gửi thường qua Telegram tiếp tục vào Local.
2. Ghi lại mã các job queued và active. Hủy job cần dừng ngay trên dòng tương ứng hoặc qua `POST /api/cloud/job/cancel` với `{"job_id":"…"}`. Trạng thái `cancelling` nghĩa là còn phải xác minh. Nếu provider chưa phản hồi, kiểm tra lại trạng thái và thử hủy cùng mã job. Với job đã kết thúc còn cleanup, giữ worker Cloud chạy để đối soát định kỳ trong lúc admission khóa.
3. Đối chiếu từng session với job/attempt. Dùng lịch sử cleanup và `GET /api/cloud/ledger?job_id=…` để xác nhận thu hồi, xử lý phần CU chưa đối soát và ghi số dư thực tế. Dừng theo mã job để các session ngoài đợt thử tiếp tục hoạt động.
4. Nếu buộc phải trả mã về bản trước, đợi cửa sổ idle và dừng tiến trình ghi. Chạy bản phát hành đã lưu từ checkout hoặc gói riêng, với chế độ Cloud tắt. Giữ DB và video hiện có. Lưu bản sao SQLite nhất quán bằng SQLite Backup nếu cần điều tra, và giữ lịch sử ledger để đối soát. Tránh dùng reset working tree hoặc chép đè DB đang dùng.

## Biên bản diễn tập cô lập

Dùng SQLite tạm và adapter giả, tiêu thụ 0 CU. Bộ hồi quy Cloud và các bài kiểm tra trong workflow CI đạt 503 ca trên cả Python 3.10 và 3.12, mỗi bản có một ca quarantine có sẵn. Bộ Node đạt 13 ca. `tests/test_cloud_phase8_rollout.py` kiểm tra các trường hợp sau:

- Hai tiến trình logic dùng hai kết nối SQLite tranh một lượt. Cả luồng claim tức thì và worker chỉ có một job được giữ chỗ CU.
- Khóa lúc job còn chờ, mở lại DB và từ chối ticket cũ. Retry cùng idempotency trả job cũ và giữ nguyên deadline.
- Khóa lúc có session của job, mô phỏng lần thu hồi đầu thất bại rồi thử lại. Chỉ session thuộc job bị dừng, ledger còn lịch sử và xác nhận thu hồi thành công.
- Runner con thoát đột ngột sau khi đăng ký session. Bản khởi động lại chờ lease hết hạn, thu hồi đúng session mồ côi và cập nhật ledger. Session độc lập được giữ nguyên.
- Job Telegram có lease còn hạn sống qua lần worker nền khởi động. Dashboard báo deadline, lỗi QC, độ trễ, cleanup và ledger chỉ bằng mã cảnh báo và mã job.

## Nhóm review

| Nhóm | Kiểm tra và phạm vi |
|---|---|
| Adapter và xác thực | Test bảo vệ biên lệnh, Host/Origin, token và redaction. Các thao tác vòng đời thật nằm trong hồ sơ Canary. |
| Vòng đời và hủy | Kiểm tra SQLite, lease, hủy, ownership và cleanup. Store chứa cả giao dịch giữ chỗ CU vì claim và reservation cần nguyên tử. |
| Ledger | Kiểm tra định lượng CU, giữ chỗ khi tranh claim, cleanup và giao ngày. So số dư thật theo hồ sơ Canary. |
| Chất lượng và thành phẩm | Kiểm tra môi trường, ASR, phụ đề, CapCut TTS, QC và xuất file. Phạm vi GPU thật theo mẫu Giai đoạn 7. |
| Dashboard và Telegram | Kiểm tra token, status, job idempotency, lựa chọn Local/Cloud và cách bật một lượt. |
| Canary và runbook | Ma trận nghiệm thu từ các giai đoạn trước và diễn tập mới trong SQLite tạm. Xác nhận lượt thử bản phát hành trước khi mở rộng. |
