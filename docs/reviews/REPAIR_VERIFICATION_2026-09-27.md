# Kiểm tra và sửa AI Video Research — 27/09/2026

## Kết luận
Bản sửa đã được build và chạy tại http://localhost:3000. Chưa công bố FULL GREEN hoặc sẵn sàng tìm hàng loạt ổn định trên cả hai nền tảng.

- Douyin: truy vấn **dụng cụ bóc sticker**, yêu cầu 20, lưu **20 video thật**, quét 34 lượt ứng viên; không có lỗi/CAPTCHA trong lượt này.
- Xiaohongshu: cùng truy vấn, yêu cầu 20, lưu **2 video thật**, quét 168 lượt ứng viên, 164 lượt bị lọc. Phiên đăng nhập được nhận diện đúng; không gặp 500 hay CAPTCHA trong lượt này.
- Thử thêm XHS `贴纸镊子分享` / `贴纸镊子测评`: yêu cầu 10, lưu 1, quét 57 lượt ứng viên, 55 bị lọc. Không dùng thử nghiệm này để thay bộ từ khóa mặc định.
- Đây là kiểm tra bằng metadata/API và hiển thị giao diện, chưa phải đánh giá nội dung từng khung hình, độ chính xác nhiều chủ đề, hoặc tải đồng thời 100 video.
- Số lượt ứng viên có thể bao gồm bài lặp giữa các truy vấn; không coi là số video duy nhất. Tất cả kết quả đã lưu trong ba lượt đều `is_mock=false`, `media_type=video` và khớp số bản ghi API.

## Những phần đã sửa trong đợt này
1. Bổ sung thông điệp authenticated cho cả hai browser service, giữ fallback an toàn cho trạng thái lạ. Không còn KeyError khi đã đăng nhập.
2. Không lấy ảnh cắt tùy ý hoặc canvas chung để giả làm mã QR XHS. Khi không có QR phù hợp, API trả 404 rõ ràng.
3. Nút mở XHS đưa đúng trang lên trước và kiểm tra lại trạng thái sau khi chuyển sang cửa sổ hiển thị.
4. Giữ miền có phiên đăng nhập XHS/Rednote khi dựng URL tìm kiếm. Khi HTTP lỗi, thử miền còn lại có giới hạn, không tự xóa cookie phiên. HTTP 4xx/5xx không còn bị nhận là tải thành công.
5. Với lượt tìm bật lọc độ liên quan, thu tối đa 3 lần số ứng viên còn thiếu, trần 150 mỗi truy vấn. XHS bỏ trần cũ 35 thẻ, cho phép tối đa 30 lần cuộn/250 thẻ ứng viên và chờ nội dung tải sau cuộn. Không hạ ngưỡng liên quan hay tự bịa kết quả.
6. Thông báo thiếu kết quả không khẳng định đã quét hết nền tảng. Phần lý do bị lọc hiển thị rõ tổng số và giới hạn 50 lý do.
7. Endpoint tải file dùng cùng session factory với DownloadService, tránh đọc sai database và trả 404 cho file đã hoàn tất.
8. Test suite khởi tạo database tạm trước khi import app/settings; tách khóa AI và queue mỗi test, giữ guard hash manifest vận hành. Không xóa dữ liệu lịch sử đã có. Các lần chạy test cũ trước khi áp dụng cách ly vẫn có thể để lại dữ liệu thử trong workspace; đợt này không tự xóa chúng.

## Bằng chứng kiểm thử
- `python -m ruff check --no-cache backend tests scripts`: exit 0.
- `python -m mypy backend --no-incremental --cache-dir <TEMP>`: exit 0, 76 file.
- `npm run lint`; `npx tsc --noEmit`: exit 0.
- `npm run build`: exit 0, 9 routes. Log: `logs/codex-build-20260926.log`.
- `python -m pytest -q -p no:cacheprovider -o faulthandler_timeout=45`: **508 passed, 3 skipped, 0 failed**, 133.11 giây, exit 0. Log: `logs/codex-regression-final-20260927.log`.
- Ba test live bỏ qua không được tính là crawl thật thành công. Ba lượt live trong báo cáo này chạy riêng qua ứng dụng.
- Manifest vận hành giữ cùng SHA-256 trước/sau suite cuối: `9D9DBAD833CF8C7560D33EBAE60431300856DA4A4DE252B33745D1CAEAD9611C`. Sau đó lượt tìm thật có thể ghi nối tiếp record mới; không quy nguồn thật cho lịch sử cũ.
- Chi tiết mã lượt tìm và ID video: `logs/codex-live-smoke-20260927.json`.
- Giao diện: mở thành công; kiểm tra đăng nhập XHS, gửi tìm kiếm, hiển thị 2 kết quả và thông báo giới hạn đúng. Không thấy lỗi console trong lần kiểm tra. Chưa chạy lại toàn bộ Playwright E2E/release gate trong đợt này; không dùng báo cáo gate cũ để chứng nhận bản mới.

## Việc còn cần cải thiện trước nghiệm thu hàng loạt
1. XHS hiện chủ yếu lấy tiêu đề trên thẻ, chưa lấy mô tả chi tiết cho những thẻ đã có tiêu đề. Các tiêu đề như “một chiếc nhíp tốt” thiếu ngữ cảnh sticker bị loại. Cần bổ sung đọc metadata chi tiết có giới hạn và cache, giữ trạng thái chưa đủ bằng chứng riêng; không đổi kết quả chưa xác minh thành phù hợp chỉ để đủ số lượng.
2. Giữ validator chặt cho truy vấn ghép nhíp + sticker. Nếu muốn mở rộng quyết định dựa trên mô tả/khung hình, cần đánh giá trên tập mẫu độc lập với cả dương tính và gây nhiễu, thay vì chỉ hạ ngưỡng.
3. Nghiệm thu nhiều chủ đề và lượt 50/100 sau khi cải thiện metadata; ghi tỷ lệ phù hợp và thời gian, kiểm tra hủy/tiếp tục khi phiên thật hết hạn.
4. Playwright E2E hiện vẫn trỏ backend vận hành; cần môi trường E2E riêng trước khi dùng full release gate như một bước chạy thường xuyên. Không chạy các test ghi/xóa dữ liệu trên thư viện thật.
