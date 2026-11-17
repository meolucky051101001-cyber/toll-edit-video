# Bản sửa local: ổn định vận hành và làm mờ phụ đề nguồn

Ngày 16/11/2026. Tiếp tục chat `01a1e2e3-5467-78a0-b115-2823de7e971e`.

**Cập nhật:** đã hoàn tất bước hợp nhất với source mới và kiểm thử lại. Xem
[báo cáo hợp nhất](MERGED_BLUR_STABILITY_20261116.md) và gói `local-blur-stability-20261116-merged.zip`.
Các số liệu và 11 file cần hợp nhất bên dưới mô tả bản review ban đầu; gói review cũ được giữ để đối chiếu, không dùng để triển khai.

## Trạng thái

Bản sửa nằm tại `C:\tool_v1_clean\local-blur-stability-20261116`, dựa trên commit snapshot `5dbe7ac` (mã local và các sửa chưa commit được chụp từ `C:\tool v1`). Chưa triển khai, chưa đổi cấu hình của tool đang chạy. `solid_box` vẫn là mặc định đến khi nghiệm thu hình ảnh thực; `localized_blur` là lựa chọn đã được triển khai trong bản sửa.

## Thay đổi

- API dùng lease chung với batch/Telegram, thông báo trạng thái hoạt động và giữ lease khi client ngắt kết nối. Khi dừng, chờ công việc native kết thúc trước khi nhả khóa. GPU gate không retry nhầm lỗi timeout phát sinh bên trong stage và dọn khóa nếu bị hủy trong lúc chờ.
- Dashboard tôn trọng biến môi trường đã cấp; health trả 503 khi thiếu router bắt buộc. Control/CLI dùng shutdown theo phạm vi và danh tính tiến trình; giữ Dashboard, Control và downloader độc lập khi dừng xử lý.
- Geometry không tự biến vùng đoán thành vùng đã xác minh; giữ chuyển động ngang và khoảng vắng chữ, loại nhãn cảnh/vỏ sản phẩm khỏi bước cứu caption. Tăng phiên bản geometry lên 6 để buộc tính lại dữ liệu cũ.
- `localized_blur` làm mờ vùng chữ nguồn theo thời gian OCR trước khi vẽ chữ Việt. ASS không sinh hộp nền trong chế độ này. Không nối các khoảng trống, không gộp những vị trí khác nhau thành dải rộng, không tự tạo vùng blur khi thiếu geometry.
- Bán kính/padding có giới hạn. Bán kính blur được giới hạn riêng theo kích thước mặt phẳng luma/chroma để FFmpeg xử lý được cả vùng thấp và vùng sát mép. Filtergraph dài được chuyển sang file để tránh giới hạn dòng lệnh Windows.
- Lưu kiểu che và tham số vào cấu hình job; chữ ký ASS/render phát hiện thay đổi để tạo lại. Hai API legacy cũng dùng cấu hình đóng băng và bản sao geometry trước dịch/TTS.
- Studio tạo lại ASS từ TTS và OCR nguồn, làm mờ trước crop/scale và từ chối geometry phiên bản cũ. Không chuyển đổi bằng cách làm mờ video đã nướng hộp trắng. Resume visual OCR cũng kiểm tra phiên bản trước khi tái dùng sidecar.
- Sửa test OCR thay `sys.modules` ngay khi collection, vốn làm rò cờ dừng sang các test khác. Thêm kiểm tra CPU thực trên ảnh tổng hợp và kiểm tra resume/legacy API.

## Kiểm chứng

Kết quả cuối được lưu tại `scratch/isolation/continuation.xml`: **289 passed, 1 skipped** trong nhóm 22 file test (file RC3 chỉ chọn ca lease liên tiến trình). Đây là bộ regression tập trung vào thay đổi, không phải toàn bộ suite của repository.

- Ba kiểm thử FFmpeg thực dùng CPU với dữ liệu tổng hợp: 320×180, 180×320 và vùng 2×2 trên khung 64×64. So sánh từng pixel chứng minh ngoài vùng/khoảng hoạt động không đổi; các khoảng vắng chữ không bị blur; chroma trung tính không bị đổi màu.
- Download, ASR, OCR/model, dịch, TTS, render NVENC và hành động shutdown trong test được giả lập. Runner chặn subprocess GPU/model/service, kết nối ngoài và ghi từ Python ra ngoài bản sao. Một fixture PowerShell chỉ đọc metadata giả được cho phép bằng hash chính xác của lệnh.
- Sandbox Windows chặn socketpair nội bộ của asyncio; các lần test hoàn tất dùng quyền thực thi ngoài sandbox, vẫn giữ các rào cách ly của runner. Không khởi động hay gọi endpoint của tool thật.
- Một ca đã nằm trong quarantine từ trước: `test_ocr_geometry_audit.py::SourceMaskAuditTests::test_source_lifetime_outside_speech_is_covered_without_extending_text`. Không thêm skip mới. Có hai cảnh báo thư viện cũ (`audioop`, Starlette/httpx).

## Đối chiếu trước khi triển khai

Kiểm tra đọc-only 1.114 file so với snapshot phát hiện **23 file nguồn hiện tại đã thay đổi**. Danh sách nằm trong `scratch/isolation/source-integrity.json`. Nhiều file trùng phạm vi bản sửa này, do đó không được chép đè toàn bộ thư mục hay áp dụng overlay tự động.

Gói review tại `C:\tool_v1_updates\local-blur-stability-20261116-review` chứa payload, manifest hash và trình `verify_update.py` chỉ đọc. Trình này đối chiếu cả tính toàn vẹn payload và baseline đích; báo file cần hợp nhất và trả mã lỗi nếu đích đã khác. Nó không cài đặt, dừng dịch vụ hay đổi cấu hình.

Quy trình triển khai tiếp theo: hợp nhất các file khác baseline trong một bản riêng lấy từ source mới nhất, chạy lại regression trên kết quả hợp nhất, rồi chọn cửa sổ tool rảnh để áp dụng và kiểm tra. Giữ nguyên mọi thay đổi không thuộc bản sửa.

## Nghiệm thu còn lại

Chưa dùng GPU, provider hay video thật để tránh tranh tài nguyên với tool đang chạy. Cần xem/nghe video thật trên cấu hình đích: caption di chuyển, hai dòng, sát mép, mất/hiện lại, nền sáng/tối, không caption; kiểm tra crop/mirror, độ dễ đọc của chữ Việt, thời lượng/audio và chi phí render/RAM/VRAM. Các ca CPU tổng hợp không thay cho nghiệm thu chất lượng che chữ thực tế.

Chỉ sau bước này mới đổi mặc định job mới sang `localized_blur`. Job/checkpoint đang chạy giữ snapshot; video cũ cần render lại từ nguồn. Chèn khung hàng loạt trên thành phẩm và sửa chỉ audio không được coi là thao tác chuyển hộp trắng sang blur. Phần tái tạo TTS/render cho 16 câu dịch trong báo cáo ASR trước đó chưa thực hiện ở lượt này.
