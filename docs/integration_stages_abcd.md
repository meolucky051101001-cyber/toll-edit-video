# Tích hợp và ổn định runtime — 01/11/2026

Phát triển và kiểm thử tại `C:\tool_v1_updates`; chỉ fast-forward workspace
chính khi test đạt và API dashboard xác nhận `active=false`. Không chạy lệnh
dừng/restart dịch vụ hoặc cutover/rollback, không cài/sửa package production.

## Stage A — hợp nhất Studio và Phase 7

Tại lúc bắt đầu, workspace chính thực tế ở `tool-v1`/`3b633ff`, không phải nhánh
hardening như mô tả bàn giao. Chỉ có OCR dirty; đã kiểm tra từng byte trùng
worktree phụ và blob đã commit `1bafc6d`, SHA256
`2a1d7847f4a686b21b846bccafe23966d1cc9c872d885bc153ae66b6a924e242`.
Đã bỏ đúng bản dirty trùng lặp được người dùng cho phép.

Chọn merge sạch `0e9233f` vào tuyến Phase 7 để giữ nguyên cả commit Studio và
bốn commit `05cbc76`, `7cd7da2`, `1bafc6d`, `d95eda2`; không có conflict.
20 test Studio được yêu cầu và 10 test Node pass trước khi nghiệm thu full suite.
Tag `v1-phase7-complete-20261101` là mốc tích hợp Phase 7/Studio của Stage A;
các stage B–D là commit tiếp theo trên nhánh chính.

| Stage A | Kết quả |
| --- | --- |
| Python 3.10 | 1155 passed / 0 failed / 79 skipped / 213 subtests; 3 warnings; 143,49 giây |
| Python 3.12 | 1155 passed / 0 failed / 79 skipped; 6 warnings; 118,93 giây |
| Studio / Node | 20 pytest passed; 10 Node tests passed |

Log nghiệm thu tại `scratch/stages-abcd/a-*` trong worktree phụ (ignore).
Các nhánh tạm chỉ xóa bằng `git branch -d` sau khi xác minh ancestor; không
force-delete. Mốc commit và các stage tiếp theo được cập nhật sau nghiệm thu.

## Stage B — giữ nguyên nhân lỗi dịch để retry Telegram

Lỗi strict khi không provider nào trả bản dịch nối `__cause__` cuối cùng;
lỗi quality gate vẫn không nối nguyên nhân mạng cũ. OpenAI/DeepSeek lưu lỗi
transport và HTTP có response để phân biệt 401 với 429/5xx, không log response
body. Bộ phân loại nhận quota/deadline và lỗi Gemini upstream tạm thời;
nguyên nhân permanent trong toàn chuỗi luôn được ưu tiên fail-fast.

Năm test mới gồm bốn ca nghiệm thu và ma trận OpenAI/DeepSeek, không gọi API
thật. Ca 503 chạy qua dịch strict và luồng Telegram, kiểm tra backoff 60/300/900
giây cùng giới hạn retry. Ca quality dùng bản dịch CJK và nguyên nhân 503 cũ
để bảo đảm không đưa lỗi nội dung vào hàng retry.

Nghiệm thu: focused 23 passed; full Python 3.10: 1160 passed / 0 failed /
79 skipped / 3 warnings / 224 subtests (174,37 giây); Python 3.12: 1160 passed /
0 failed / 79 skipped / 6 warnings (137,87 giây). Log `scratch/stages-abcd/b-*`.

## Stage C — lifespan, control token và thứ tự Gemini

FastAPI dùng lifespan, gọi `_startup_background_tasks()` với nguyên logic
startup cũ; giữ alias `on_startup` cho caller trực tiếp. Test lifespan mock
helper nên không khởi động tool_control, watcher hay bảo trì workspace thật.

Đã rà soát template/static: năm điều khiển stop/pause/resume/audio/worker trên
dashboard dùng wrapper fetch đầu trang, wrapper luôn thêm control token với
object headers và Headers. Timeline/workflow có token riêng. Xóa hoàn toàn
miễn trừ `X-Dashboard-Input`; test middleware không gọi các handler điều khiển
thật. Node test chạy chính wrapper từ template cho cả năm endpoint.

Có thể cấu hình `V1_GEMINI_TRANSLATION_MODELS` và
`V1_GEMINI_CONDENSATION_MODELS` bằng danh sách tên model phân tách dấu phẩy.
Danh sách override thay thế thứ tự mặc định, trim và bỏ trùng. Không đặt thì
giữ preset/ưu tiên legacy; explicit `models` của caller vẫn được ưu tiên.
Không thay `.env` production hoặc tự chọn model mới. Cache dịch bao gồm thứ tự
model đã resolve nên không dùng nhầm cache của cấu hình model cũ.

Model COOLDOWN không nhận request tới khi hết hạn; khi thành HALF_OPEN được
xếp sau ACTIVE, giữ single-probe nguyên tử qua SQLite. Nếu tất cả đang cooldown,
trả lỗi tạm thời ngay để Telegram dùng backoff, không ngủ chờ hoặc probe sớm.
Test mới dùng SQLite tạm và HTTP mock, không đọc DB circuit production.

Nghiệm thu: focused 43 passed / 1 skip quarantine có sẵn; Node 11 passed.
Full Python 3.10: 1168 passed / 0 failed / 79 skipped / 1 warning / 259 subtests
(155,52 giây); Python 3.12: 1168 passed / 0 failed / 79 skipped / 4 warnings
(144,66 giây). Đã hết hai warning FastAPI `on_event`.
Log `scratch/stages-abcd/c-*`; không thêm skip hoặc giảm assertion cũ.
