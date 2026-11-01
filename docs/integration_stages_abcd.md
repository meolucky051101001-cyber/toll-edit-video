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

## Stage D — health check chỉ đọc sau cutover

Từ `C:\tool v1`, sau khi người dùng tự dừng/chuyển/khởi động lại:

```powershell
.\backend\venv\Scripts\python.exe -B scripts\verify_runtime_health.py
```

Script mặc định kiểm tra Python **3.12** tại `backend/venv/Scripts/python.exe`,
`python -m pip check`, preflight `--interface all --read-only` (ready=true và
25/25 pass), dashboard :8088 và control :8090. In status/active cùng VRAM/RAM;
active=true không phải lỗi health. Nếu control chưa có /health (404/405), kiểm
tra TCP và ghi rõ giới hạn này; HTTP 500 không được coi là khỏe nhờ fallback.
Chỉ gọi GET, không launch/stop/restart dịch vụ, không đổi tên/cài package venv.

Ngân sách mặc định 5 giây dùng chung cho các probe chạy song song. Preflight
read-only chạy import CUDA và OCR độc lập song song, giữ đủ 25 kiểm tra và thứ
tự report. Nếu máy cold hoặc tải cao không kịp, báo FAIL/incomplete, exit 1;
có thể chạy lại với `--timeout 30`. Timeout chỉ thu hồi subprocess probe do
script tạo, không tìm/kill PID dịch vụ. OCR có deadline ngắn hơn probe cha.

Quyền directory ở chế độ read-only dựa trên directory tồn tại và `os.access`,
không tạo/xóa file kiểm tra; đây không phải bằng chứng ghi thực tế qua ACL.
Preflight mặc định không thêm `--read-only` vẫn giữ write probe cũ. Không in
secret, response body hay output thô của pip/preflight trong health report.

Có thể xác minh staging trước cutover mà không đổi runtime:

```powershell
.\backend\venv_py312\Scripts\python.exe -B scripts\verify_runtime_health.py --python .\backend\venv_py312\Scripts\python.exe
```

Lượt read-only trên cấu hình production bằng staging 3.12.14 đạt PASS trong
3,66 giây, pip check sạch, preflight ready=true 25/25, API idle/active=false;
8090 chỉ xác minh TCP. Kết quả staging không khẳng định các dịch vụ đang chạy
đã dùng Python 3.12. Không thực hiện cutover hoặc restart trong đợt này.

Đối chứng venv production hiện tại: Python 3.10.11, pip check có conflict nên
health trả FAIL trong 2,48 giây như kỳ vọng trước cutover; preflight vẫn 25/25,
hai dịch vụ vẫn reachable. Không sửa package 3.10 để che trạng thái này.

Nghiệm thu cuối: 10 test health/preflight mới pass. Full Python 3.10:
1178 passed / 0 failed / 79 skipped / 1 warning / 261 subtests (127,10 giây).
Full Python 3.12: 1178 passed / 0 failed / 79 skipped / 4 warnings (111,66 giây).
Node 11 passed / 0 failed. Log `scratch/stages-abcd/d-*`. Các warning còn lại
từ dependency testclient/audio cũ; không còn warning `on_event` của main.py.

Commit theo stage: A `6f8b91c`, B `43589ef`, C `3000125`; Stage D là commit
`feat(v1): add bounded read-only runtime health verification` chứa mục này.
Sau nghiệm thu, chỉ fast-forward hai ref tool-v1/pre-phase7-hardening khi
dashboard `active=false`, giữ tag Stage A tại `6f8b91c`.
