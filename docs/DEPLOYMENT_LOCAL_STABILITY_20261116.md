# Triển khai bản ổn định local — 16/11/2026

## Cập nhật sau nghiệm thu

Người dùng đã xem video thành phẩm và chấp nhận chất lượng ngày 16/11/2026.
Checkout `C:\tool v1` đã chứa bản sửa biên phụ đề tại commit `40ea82d`:
geometry version 8, blur render version 4 và QC tại điểm chuyển câu.
171 test mục tiêu đạt; CI Node, Python 3.10 và Python 3.12 đều đạt tại commit này.
Kết quả video và giới hạn kiểm chứng nằm trong
[báo cáo sửa biên phụ đề](CAPTION_BOUNDARY_FIX_20261116.md).

Đã bật cấu hình trên máy đang sử dụng cho **job mới**:

- `D:\workspace\bot_system\canvas_settings.json`: `sub_cover_style=localized_blur`.
- `D:\workspace\bot_system\v1_feature_flags.json`: `V1_VISUAL_SUB_TO_SPEECH=true`.

Đã đọc lại bằng các module cấu hình của ứng dụng và xác nhận hai giá trị.
Chỉ hai trường này được thay đổi; snapshot của job cũ vẫn giữ cấu hình riêng.
Mặc định trong mã nguồn vẫn là `solid_box` và tắt visual-sub-to-speech để
các cài đặt khác chủ động lựa chọn. Runtime config không đưa vào Git hoặc ZIP.

Bản sao nguyên trạng hai file và receipt nằm tại
`C:\tool_v1_updates\accepted-config-20261116-232526`.
Để hoàn tác lựa chọn trên máy, khi không có job active hãy đặt lại hai trường
thành `solid_box` và `false`; chỉ khôi phục toàn file backup nếu không có thay
đổi cấu hình mới hơn cần giữ.

Dịch vụ chính vẫn tạm dừng; hai tiến trình downloader độc lập được giữ nguyên.
Cấu hình mới có hiệu lực khi mở lại Tool V1 và tạo job mới.

## Lịch sử triển khai ban đầu

Đã áp dụng 32 file của bản hợp nhất vào `C:\tool v1` sau khi kiểm tra hash
baseline của 1.132 file. Các thay đổi local đã có về dịch/OCR, chọn giọng,
Telegram và cloud dispatch được giữ lại cùng các bản sửa runtime, shutdown,
Studio và localized blur. Nhánh nền `tool-v1` đã được fetch và khớp
`origin/tool-v1` tại `4b2d234acc92fead3fc097e570de523089290aa4` trước khi triển khai.

## Thay đổi và trạng thái vận hành

- API dùng lease chung; hủy/timeout chờ worker native kết thúc trước khi nhả tài nguyên.
- Dừng tiến trình theo phạm vi Tool V1; có kiểm tra scope và trạng thái GPU gate.
- `localized_blur` dùng vùng OCR đã xác minh, đúng khoảng xuất hiện; không thêm hộp nền ASS.
- Giữ snapshot cấu hình và vô hiệu cache geometry cũ trong resume/Studio.
- Giữ mặc định `solid_box`; chưa đổi mặc định blur khi chưa nghiệm thu video thật.
- Bổ sung bảy module kiểm thử regression vào CI Windows Python 3.12/3.10.

Job tracker idle trước triển khai. Đã dừng 18 tiến trình Tool V1 đã xác minh;
hai tiến trình downloader độc lập được giữ nguyên. Sau triển khai, dịch vụ vẫn
được pause. Không mở lại Control vì bootstrap hiện tại tự bật bot/xử lý.
Không gọi provider, thuê GPU cloud hay chạy pipeline thật trong bước triển khai này.

## Kiểm thử trên checkout đích sau cập nhật

| Bộ kiểm thử | Đạt | Bỏ qua | Lỗi |
|---|---:|---:|---:|
| Runtime, shutdown, geometry, blur, API, Studio, resume, ENV, caption evidence | 318 | 0 | 0 |
| Release, guards, cloud lifecycle/dispatch, speech translation repair | 88 | 13 | 0 |
| CI bổ sung, dual voice và QC | 106 | 0 | 0 |
| Node frontend/dashboard | 13 | 0 | 0 |
| Tổng | **525** | **13** | **0** |

13 skip là quarantine sẵn có trong speech translation repair. Có hai cảnh báo
deprecation (audioop và Starlette/httpx). Đây là các nhóm kiểm thử mục tiêu,
không phải toàn bộ repository. Ba test FFmpeg CPU thực xác minh pixel trên
video tổng hợp; model/provider/cloud và shutdown dùng mock. Python chạy qua
runner cô lập dữ liệu tạm, chặn kết nối provider và subprocess GPU/service.

Kết quả máy đọc nằm tại `scratch/deployment-tests/post-update.xml`,
`preservation.xml` và `ci-extra.xml`; log và runner nằm cùng thư mục (không commit).
Node chạy bằng `node --test` cho toàn bộ `tests/js/*.test.js`.

## Gói triển khai và khôi phục

- Gói: `C:\tool_v1_updates\local-blur-stability-20261116-merged.zip`.
- SHA256: `1282c48f185ab4185d8e790549ffa6f6d3f6b64787e2637a1490cf00e011f0f2`.
- Backup: `C:\tool_v1_updates\deployment-20261116-blur` chứa file trước áp dụng,
  diff staged/unstaged, trạng thái Git, service flags và receipt shutdown/triển khai.
- Khi khôi phục cần dừng đúng dịch vụ, kiểm tra không có job active và đối chiếu
  thay đổi phát sinh sau triển khai trước khi chép lại file; không reset toàn bộ checkout.

Nghiệm thu video thật, đồng bộ âm thanh/hình, RAM/VRAM và NVENC là bước riêng.
Không suy ra kết quả GPU/provider từ kiểm thử CPU hoặc mock ở trên.

Sau bước triển khai, người dùng cung cấp video để test. Các lượt chạy đầu và
lỗi phát hiện khi đó được giữ trong [báo cáo kiểm thử video](VIDEO_BLUR_CHECK_20261116.md).
Lỗi biên thời gian đã được sửa và video cuối được người dùng nghiệm thu;
trạng thái triển khai hiện tại được ghi ở đầu tài liệu này.
