# Nghiệm thu điều khiển dashboard — 17/11/2026

Phạm vi: đổi mode/video model, ASR chung, model theo độ dài video, Dừng Xử Lý và Dừng Tool. Làm tại worktree `dashboard-stability/tool v1`; giữ nguyên các thay đổi remediation đã có, không stage/commit và không khởi động lại dịch vụ live.

## Hành vi sau sửa

- Mode/ASR/profile cập nhật giao diện ngay trong lượt click; người dùng vẫn chọn tiếp được khi request trước chưa trả lời.
- Mỗi cài đặt có hàng đợi lưu riêng, bỏ qua lựa chọn trung gian chưa gửi. Phản hồi cũ không ghi đè lựa chọn mới; lỗi mới nhất hoàn tác về giá trị đã được server xác nhận và hiển thị lỗi thật.
- GET cấu hình trả chậm không ghi đè lựa chọn vừa thay đổi. Một batch mới phải chờ các lựa chọn được lưu; chờ quá 10 giây hoặc lưu lỗi thì chưa tạo batch. Retry một batch đã lưu vẫn giữ cấu hình đóng băng ban đầu.
- Readiness SenseVoice và thao tác lưu feature flags chạy trong threadpool FastAPI. Khóa thread và khóa liên tiến trình bảo vệ toàn bộ read/merge/write, tránh ghi mất cài đặt khác khi Dashboard/Bot cùng lưu.
- Dừng Xử Lý gửi POST ngay, bỏ hộp xác nhận chặn trước request. Trạng thái đang dừng hiển thị ngay và được giữ khi nhận 202; lỗi HTTP/mất xác nhận cho phép thử lại, không báo đã dừng.
- Đọc trạng thái, ghi shared stop flag và ghi tracker được gom trong một transaction chạy ngoài event loop. Tracker idle không bị đánh dấu chạy lại chỉ vì task cũ chưa kết thúc.
- Dừng Tool/off_all lưu stop intent trước ACK 202, rồi drain/shutdown ở thread nền. Khóa điều khiển giữ đến khi cleanup xong; lỗi cleanup được báo và chặn bật lại cho đến khi yêu cầu dừng lại được xác nhận.

Cấu hình model áp dụng cho job mới; job đang chạy giữ snapshot của nó. Yêu cầu dừng có hiệu lực ngay ở stop intent/admission và UI; công việc native kết thúc theo checkpoint/drain hiện hữu. Chỉ hiển thị hoàn tất sau khi xác nhận cleanup, không ép nhả lease hay kill ngoài phạm vi sở hữu.

## Kiểm chứng cuối

| Gói | Kết quả |
| --- | --- |
| Python: controls responsiveness, toàn bộ acceptance API, snapshot/ASR/QC, shutdown scope, tool control, feature flag persistence | 51 passed |
| JavaScript: toàn bộ tests/js/*.test.js và dashboard progress | 42 passed |
| git diff --check | Không có lỗi |

Python dùng `C:/tool v1/backend/venv/Scripts/python.exe`, với dependency test có sẵn trong `scratch/dashboard-test-deps` qua PYTHONPATH tạm thời, khôi phục sau lệnh. Không cài/sửa dependency production. Test mới bao phủ readiness/stop I/O chậm trên cùng event loop, 12 lần ghi cờ đồng thời, setter shared stop chậm, task cũ sau tracker idle, ACK trước cleanup và chặn restart.

Lệnh JavaScript: `node --test tests/js/*.test.js tests/test_dashboard_progress.cjs`. Lần cuối chạy trên snapshot tạm `C:/Users/admin/AppData/Local/Temp/dashboard_controls_verify_8txcl73p`, sau khi đối chiếu SHA-256 tất cả source/test được sao chép; bổ sung hai dependency timeline.js/timeline_time_core.js và cũng đối chiếu hash. Snapshot chỉ phục vụ kiểm tra vì bộ duyệt quyền hết thời gian khi khởi chạy lệnh trong worktree. Các test mới kiểm tra lựa chọn cuối, rollback lỗi, hydration cũ, đổi model rồi tạo batch, chặn batch khi lưu lỗi, gửi stop ngay và giữ trạng thái đang dừng.

Kiểm tra rộng hơn đã ghi nhận hai test Phase 9 chưa đạt: `test_unified_pipeline_lock_identity_and_conflict` dùng kỳ vọng admission cũ; `test_gate4_controller_reads_and_sends_dashboard_token` giữ fixture status active suốt thời gian drain. Không tính hai test đó vào gói 51 passed và chưa sửa fixture trong phạm vi thay đổi này. Lần chạy bằng Python hệ thống thiếu deep_translator; test OCR visual activity riêng có sai lệch ngưỡng ảnh giả. Kết quả nghiệm thu ở trên dùng đúng venv/gói liên quan, không suy diễn rằng toàn bộ repository đã pass.

## File của lượt này

Production: backend/main.py; backend/templates/dashboard.html; backend/tool_control.py; backend/v1_feature_flags.py.

Regression: tests/test_dashboard_controls_responsiveness.py; tests/test_tool_control_responsiveness.py; tests/js/dashboard_controls.test.js. Điều chỉnh fixture của tests/js/batch_recovery.test.js để tiêm hàm chờ cấu hình khi test riêng luồng batch.

Chưa kiểm thử thao tác trên phiên dashboard live, chưa restart dịch vụ, chưa chạy lại soak/native GPU. Các kết quả soak/remediation trước không được dùng để chứng minh thay đổi điều khiển lần này.
