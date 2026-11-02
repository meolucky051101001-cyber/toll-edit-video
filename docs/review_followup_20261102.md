# Sửa các điểm hậu kiểm cleanup — 02/11/2026

Mốc trước sửa: `6a6ce46`; review xác nhận số liệu cleanup đến `f77a2af` nhưng
phát hiện thiếu các ca âm ở shutdown, release và tương thích CLI/CI.
Triển khai tại worktree updates, không cài/sửa package, không xóa dữ liệu,
không chạy stop/start/rollback dịch vụ thật để kiểm thử.

| Điểm | Thay đổi | Bằng chứng |
| --- | --- | --- |
| R1 | Task phải có đúng action executable, script, service và root trước khi /End; task Ready không bị /End; lỗi đọc/đổi action bị từ chối | Negative tests root khác, action sai/nhiều, AccessDenied, race task tự thoát; kiểm metadata thật chỉ đọc |
| R2 | Không suy đoán cwd; so path entrypoint chính xác, yêu cầu executable và creation time | Process thiếu cwd/exe/time và main.py ở thư mục khác không được chọn |
| R3 | Đọc workspace theo environment resolver chung, env override .env; đọc telemetry ở cả control layout, chặn status JSON hỏng | Job active ở workspace chỉ cấu hình trong .env vẫn chặn stop |
| R4 | service_control dùng chung path và intent cho shutdown, launcher start, supervisor, admission gate và Tool Control | Stop/resume round-trip layout cũ/mới/custom; giữ downloader độc lập; restart mode không tạo stop intent |
| R5 | Kiểm lại create_time, executable và argv trước terminate; PID đổi identity được báo ambiguous và không giả cập nhật telemetry đã dừng | Các ca lệch 1 ms/1 giây, đổi exe/argv và telemetry còn nguyên khi không dừng được |
| R6 | CLI shim chuyển tiếp main khi chạy bằng -m | --help trả usage; option sai trả exit 2; canonical và nested đều chạy cùng hợp đồng |
| R7 | ZIP/manifest dùng cùng validator cho configured secret và credential literal; lỗi đọc file không được coi là sạch | Fixture không có .env vẫn bị bác; không in secret, không tạo ZIP khi bị bác |
| R8 | Git lỗi fail-closed; không gói file untracked bằng filesystem fallback | Git return 128/OSError và repo rỗng; source export không có Git vẫn được hỗ trợ |
| R9 | Bỏ hướng dẫn hard reset; rollback theo commit, giữ checkpoint và idle guard | Docs không hướng dẫn bật lại script ghi đè hoặc kill rộng |
| R10 | Adapter PyAV độc lập, idempotent, closure giữ original opener; worker/transcription dùng chung; CI test gọi chính adapter này bằng fake PyAV | Probe không cài av vẫn chạy test; tham số khác, lỗi decode và reload được bảo toàn |

Đã kiểm chứng 84 test trọng tâm pass; Node 11 pass. Probe Scheduled Task thật
chỉ đọc: production root nhận đúng 5 task Running, worktree root bị từ chối;
không gọi controller/terminator thật. Bằng chứng và log nằm ở
`scratch/review-fixes-20261102/` (ignore).

## Khởi động và dừng sau cập nhật

- Stop vẫn qua các launcher hiện có; --preview không thực hiện thao tác dừng.
- Start rõ ràng dùng `v1_shutdown.py --resume --scope all` để clear intent V1
  trên các control directory thực tế trước khi launch. Lệnh resume chỉ clear
  intent, không launch/terminate hoặc thay Enabled state của Scheduled Task.
- `start_telebot_downloader.bat` chỉ resume scope downloader; không mở lại V1.
- Scope all không clear downloader.disabled của bot tải độc lập; chỉ cutover
  hoặc yêu cầu include-downloader mới bao gồm thành phần này.
- Chưa chạy stop/start round-trip thật hoặc render video người dùng trong lượt
  sửa code. Full suites không thay cho nghiệm thu vận hành có chủ đích đó.

## Kết quả cuối

| Kiểm tra | Kết quả |
| --- | --- |
| Full suite Python 3.12.14 | 1255 passed, 79 skipped, 4 warnings; 102.93 giây |
| Full suite Python 3.10.11 | 1255 passed, 79 skipped, 1 warning, 261 subtests passed; 108.56 giây |
| Test trọng tâm shutdown/release/CLI/ASR | 84 passed |
| Node | 11 passed, 0 failed |
| CI collection local | 210 tests trong 19 file; không phải kết quả chạy GitHub Actions |
| PyAV test khi chặn import av | 4 passed; adapter thật được gọi bằng fake module |
| PowerShell syntax | 3 file pass |
| ZIP và manifest dry-run | Cùng 567 file; security hygiene PASS; không tạo archive/backup |
| Runtime trước đồng bộ (chỉ đọc) | Python 3.12.14, pip check sạch, preflight 25/25; dashboard idle/active=false; Tool Control TCP reachable |

79 test skipped là danh sách quarantine có sẵn, không thêm skip để đạt kết quả.
Kiểm tra runtime xác nhận phiên bản dịch vụ đang chạy trước đồng bộ; bản sửa
mã nguồn sẽ được nạp đầy đủ ở lần restart được cho phép tiếp theo. Lượt này
không restart dịch vụ hoặc push GitHub.
