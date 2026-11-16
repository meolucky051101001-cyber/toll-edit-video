# Hợp nhất bản sửa blur và vận hành — 16/11/2026

> Cập nhật triển khai: bản hợp nhất đã được áp dụng vào `C:\tool v1` ngày
> 16/11/2026 theo yêu cầu người dùng. Nội dung bên dưới ghi lại bước hợp nhất
> trước triển khai; trạng thái mới và kiểm thử tại checkout đích nằm trong
> [báo cáo triển khai](DEPLOYMENT_LOCAL_STABILITY_20261116.md).

## Kết quả bước này

Đã hợp nhất bản sửa riêng với mã hiện có tại `C:\tool v1`, chỉ ghi vào
`C:\tool_v1_clean\local-blur-stability-20261116`. Không dừng/restart dịch vụ,
không thay mã hoặc cấu hình của tool đang chạy và không gọi provider/GPU.

11 file giao nhau đã được xử lý: 9 file có xung đột nội dung và 2 file ghép tự động.
Các sửa khác của source mới được đưa vào bản kiểm thử để kiểm tra đúng ngữ cảnh,
nhưng không đưa lại vào payload nếu nội dung đã giống source đích.

## Quyết định khi hợp nhất

- Giữ loader ENV của source mới ở đầu `main.py`, trước khi các module khác chốt đường dẫn; bổ sung lease/API health và cấu hình blur đã đóng băng.
- Giữ kiểm soát lease chặt của source mới: cấu hình hai worker không cho phép hai job lấy chung lease trong cùng process.
- Giữ GPU gate bất đồng bộ, dọn khóa khi hủy trong lúc đợi và không retry timeout từ body. Giữ bảo vệ mutex khi reentrant và chờ công việc native hoàn tất sau timeout/cancel, kể cả cancel lặp lại.
- Kết hợp source-mask không mở rộng qua khoảng vắng chữ với ASS không có hộp nền khi chọn blur. Giữ observation có khớp transcript khi caption di chuyển; không cứu nhãn cảnh không đáng tin.
- Giữ geometry chưa xác minh ở trạng thái unresolved cả trong chế độ cảnh báo. Giữ phiên bản geometry 6, kiểm tra cache cũ của visual resume và Studio.
- Giữ các sửa mới về tra thư mục job theo manifest, giọng chỉ định từ API, dịch/OCR evidence, hàng đợi Telegram, cloud dispatch và kiểm tra đóng gói release.
- Giữ module test OCR canonical, gộp test của cả hai phía; sửa test bootstrap ENV để không phụ thuộc số dòng source.

## Kiểm thử trên bản hợp nhất

| Bộ kiểm thử | Đạt | Bỏ qua | Lỗi |
|---|---:|---:|---:|
| Runtime, shutdown, geometry, blur, API, Studio, resume, ENV, caption evidence | 318 | 0 | 0 |
| Bảo toàn release, guards, cloud lifecycle/dispatch, speech translation repair | 88 | 13 | 0 |
| Tổng | **406** | **13** | **0** |

13 skip nằm trong nhóm speech translation repair đã có quarantine; không thêm skip mới.
Ca source lifetime từng bị bỏ qua ở bản review trước nay chạy và đạt trên mã hợp nhất.
Có cảnh báo `audioop` deprecated và Starlette/httpx; đây không phải toàn bộ suite repository.

Ba test FFmpeg CPU thực vẫn kiểm tra pixel ở video tổng hợp ngang/dọc và vùng cực nhỏ.
Các tác vụ model/provider/download/cloud/shutdown dùng mock. Runner cách ly dữ liệu tạm,
chặn subprocess GPU/model/service và kết nối dịch vụ thật; asyncio chạy ngoài sandbox
do sandbox Windows chặn socketpair nội bộ. Fixture PowerShell chỉ trả metadata giả.

Kết quả máy đọc: `scratch/isolation/merged.xml`, `scratch/isolation/preservation.xml`.
Backup trước hợp nhất và quyết định từng file nằm trong `scratch/merge-20261116/`.

## Gói mới và kiểm tra baseline

Dùng gói `C:\tool_v1_updates\local-blur-stability-20261116-merged.zip`.
Gói có payload tối thiểu so với source hiện tại, hash trước/sau mỗi file,
báo cáo test, receipt baseline và trình `verify_update.py` chỉ đọc.

Trước khi triển khai, chạy Python với `verify_update.py` và đường dẫn checkout đích.
Nếu `baseline_compatible` là false, dừng và hợp nhất lại các file được liệt kê.
Checker không cài đặt hay cho phép triển khai chỉ vì hash khớp. Gói review cũ không còn là baseline triển khai.

## Bước còn lại trước khi áp dụng

Giữ mặc định `solid_box` đến khi nghiệm thu video thật. Cần kiểm tra blur trên mẫu nguồn
có caption di chuyển/mất-hiện, chữ hai dòng/sát mép, nền sáng/tối và crop/mirror; nghe/xem
đồng bộ thành phẩm, đo RAM/VRAM và thời gian NVENC. Đây là nghiệm thu riêng khi có cửa sổ
không tranh tài nguyên với job đang chạy. Chưa tái tạo TTS/render cho 16 câu dịch đã sửa trước đó.

Sau nghiệm thu, mới áp dụng bản đã kiểm tra trong cửa sổ tool rảnh và đổi mặc định cho job mới.
Job đang chạy/checkpoint cũ giữ snapshot; các video có hộp trắng đã nướng phải render lại từ nguồn.
