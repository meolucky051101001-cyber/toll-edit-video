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
