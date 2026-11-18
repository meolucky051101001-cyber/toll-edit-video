# Bản cập nhật Tool V1 — 17/11/2026

Bản này hợp nhất local `9dbfc0a` với Cloud phase 8 `be62883`, sau đó sửa các lỗi trong [review ngày 17/11](REVIEW_TOOLV1_20261117.md). Báo cáo review được giữ nguyên như bằng chứng trước sửa.

## Những thay đổi đã hoàn tất

| Finding | Hành vi sau sửa |
|---|---|
| R1 — mất hàng chờ khi recovery lỗi | Parse, kiểm tra và chuẩn bị toàn bộ snapshot trước khi nạp. Lỗi giữ snapshot và recovery barrier. Dashboard gửi lệnh reorder riêng, không ghi đè snapshot do bot quản lý. |
| R2 — job đã xóa chạy lại | Lọc tombstone khi restore; watcher xác nhận xóa/reorder kể cả lúc worker bận. Clear lưu tombstone trước khi bỏ cờ yêu cầu và giữ job đang chạy, kể cả lỗi ghi snapshot rồi restart. |
| R3 — sai timing blur VFR | Dùng chỉ số frame và PTS thật do decoder trả về; kiểm tra seek/index/clock. Geometry version 9 loại cache cũ. Decoder không có clock hợp lệ bị từ chối. |
| R4 — health báo thiếu router | Kiểm tra route bằng hợp đồng `matches` của Starlette, hỗ trợ router lồng của FastAPI 0.162 và phương thức GET. |
| R5 — Cloud hở chữ dù QC PASS | Đo geometry từ detector ở hai tỉ lệ, không loại vùng chữ chỉ vì độ tin cậy nhận dạng thấp. Phân biệt chưa xác minh với không có caption. QC yêu cầu evidence geometry cùng độ phủ ASS. |
| R6 — shortcut chạy Cloud cũ | Launcher dùng runner và Python trong cùng thư mục dự án; Dashboard chính có router Cloud phase 8. |

Giữ lại các bảo vệ local khi hợp nhất Cloud: dừng provider phải có xác nhận; khóa rollout và hủy job trước emergency stop; thu hồi session trước xuất thành phẩm; tính CU cả thời gian chờ dừng. Migration SQLite giữ chi phí cũ kể cả lịch sử job đã xóa, không cộng trùng; phiên chưa xác minh cleanup vẫn chặn admission và tiếp tục tính chi phí.

Snapshot/retry/recovery giữ cả lựa chọn Cloud phase 8 và nguồn local. Hai test nền được sửa theo hợp đồng hiện hành: câu rút gọn trong ngân sách từ, và tranh chấp batch lease thật; thêm regression từ chối câu vượt ngân sách.

Các fixture API/queue được cập nhật để kiểm tra command chưa xác nhận và snapshot do bot quản lý. Fixture dịch không còn import lại module dưới `requests` giả rồi để lại tham chiếu ảnh hưởng test sau; kiểm thứ tự gây lỗi trên Python 3.10 đã đạt.

## Kiểm chứng

Kết quả full suite Python 3.12 và bộ compatibility Python 3.10 được ghi trong hồ sơ release cùng commit và SHA256. Các lượt test đều dùng workspace tạm và runner chặn mạng/provider; không gửi Telegram hay gọi TTS/Colab để kiểm thử.

- Regression VFR dùng video FFmpeg thật đổi 30→10 và 10→30 fps, đối chiếu mask ở từng frame.
- Cloud: kiểm import bundle độc lập; source evidence thiếu/không rõ bị chặn; cleanup thất bại không xuất file; ledger cũ được kiểm migration và restart.
- Node frontend: 13 passed. `pip check`: không có dependency hỏng. `git diff --check`: đạt.
- Full suite Python 3.12: **1.759 passed, 78 skipped**, 260,87 giây. Skip gồm 72 quarantine có sẵn, 2 Remotion opt-in, 3 benchmark fixture không có và 1 Satori sidecar không cài trong worktree kiểm thử.
- Compatibility Python 3.10: **713 passed, 69 subtests passed**, 113,84 giây; gồm CI lane cùng các kiểm thử Cloud và queue thay đổi.
- Preflight runtime đang dùng: **25/25 pass**.
- Không thêm quarantine hoặc bỏ test đang lỗi để làm xanh suite.

## Bằng chứng video Cloud

Dùng lại video nguồn và audio Cloud ngày 16/11, render CPU thành file mới, không tạo TTS hoặc thuê GPU. Kiểm 131 frame: 114 frame có chữ và 17 frame trống; trong đó 59 frame được thêm để tinh chỉnh chuyển vùng. Đủ evidence cho 15 cue. Seek/index/PTS được đối chiếu lại cho cả 131 quan sát.

Video mới H.264/AAC, 720×1038, 30 fps, 23,488 giây, giải mã toàn bộ không lỗi. SHA256:

`b572797a28284a4985b5d08ea4da1ae28deea310114187836f88556f62646269`

Ảnh nguồn/trước/sau tại 0,500s và 18,500s xác nhận hai mép chữ Trung đã được che. QC thật chấp nhận video mới, từ chối video cũ do thiếu geometry evidence. Hồ sơ tại `C:\tool v1\scratch\review_20261117_cloud_fixed` gồm ảnh, video, receipt render, QC và decoder clock.

Giới hạn: geometry lấy mẫu mỗi 0,35 giây và tinh chỉnh khi vùng chữ thay đổi; chưa phải OCR mọi frame. Kiểm hình trực tiếp ở hai mốc lỗi đã biết. Không coi mẫu này là nghiệm thu mọi video hoặc chất lượng bản dịch/audio mới. Cloud phase 8 vẫn dùng cơ chế rollout có kiểm soát; lượt sửa này không mở thêm lượt GPU trả phí.

## Triển khai và khôi phục

Release và receipt đặt tại `C:\tool_v1_updates`. Quy trình triển khai kiểm HEAD/snapshot nguồn, sao lưu mã/config/queue/SQLite, từ chối khi có job bận, dừng đúng Dashboard/Telegram/Tool Control rồi fast-forward commit đã kiểm thử. Downloader độc lập không thuộc phạm vi dừng.

Backup: `C:\tool_v1_updates\deployment-20261117-review`. Receipt ghi commit, hash từng file và trạng thái dịch vụ. Chỉ khôi phục database từ backup khi đã dừng dịch vụ; không chép snapshot cũ đè lên queue hoặc lịch sử mới phát sinh sau cập nhật. Source baseline trước cập nhật là `9dbfc0a6b61dd195895905de1e068b5168f2d8a7`.
