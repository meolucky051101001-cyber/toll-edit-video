# Review Antigravity - Vòng 9

Ngày review: 2026-09-07.

## Kết luận

Antigravity đã sửa đúng phần lớn kế hoạch vòng 8. Các release gate đều đạt khi chạy tuần tự. Còn hai lỗi dữ liệu mức P2 mà test hiện tại chưa phát hiện.

## Findings

### P2 - Assignment thất bại vẫn làm hỏng trạng thái VideoResult

Tại backend/schemas/contracts.py dòng 92-126, validate_assignment=True với model validator hậu kiểm báo lỗi sau khi giá trị đã được gán. Probe xác nhận gán share_url FTP hoặc đổi platform sai đều ném ValidationError, nhưng object vẫn giữ giá trị sai và model_dump vẫn xuất nó. Test mới chỉ kiểm tra exception.

Sửa bằng model bất biến (frozen=True) và dựng model mới qua VideoResult.model_validate với dữ liệu cũ cộng updates. Không dùng model_copy(update=...) cho dữ liệu cần kiểm tra vì update không được validate. Thêm test khẳng định object cũ không đổi sau mọi cập nhật bị từ chối.

### P2 - resolve_xhs_urls có thể ghép token của bài khác

Tại backend/providers/xiaohongshu/parser.py dòng 98-127, canonical ưu tiên detail URL, nhưng khi detail thiếu token, hàm lấy candidate URL có token mà không kiểm tra hai note ID giống nhau. Hàm cũng nhận URL có path bất kỳ trên hostname tin cậy làm share URL.

Probe tạo được (canonical B, share_url A, note_id B) và một share URL /unrelated?xsec_token=X. Chỉ chọn token URL khi note_url(source_url) == canonical. Kiểm tra HTTPS, host, credentials, port và note path. Nếu hai ID khác nhau, không dùng token candidate.

## Kết quả kiểm thử

- Pytest: 100 passed, 2 cảnh báo dependency.
- Ruff: passed.
- Mypy: passed, 36 source files.
- TypeScript: passed.
- ESLint: 0 errors, 2 cảnh báo img cũ.
- Next.js production build: passed, 9 routes.
- Playwright: 10 passed khi chạy tuần tự.

## Kế hoạch vòng tiếp theo

1. Làm VideoResult bất biến và tạo helper copy có validation.
2. Rà soát mọi model_copy(update=...) của VideoResult.
3. Ràng buộc token URL phải cùng canonical note ID.
4. Thêm regression test cho trạng thái sau cập nhật lỗi và token khác ID/path.
5. Chạy gate tuần tự, rồi smoke test thật 5 video mỗi nền tảng với AI tắt.

Review không gọi Gemini, không chạy crawler thật và không ghi database ứng dụng.

