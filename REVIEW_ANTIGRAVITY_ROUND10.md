# Review vòng 10 và kế hoạch phase tiếp
Ngày: 2026-09-07
Codebase: C:/Users/admin/Projects/ai-video-research-tool

## Đã hoàn thành vòng 9
- VideoResult và VideoOut frozen=True; assignment bị từ chối không làm thay đổi URL cũ. Probe độc lập đạt.
- update_validated và update_video_result dựng model mới qua validation.
- model_copy(update=...) được override để validate.
- API videos đã chuyển cập nhật score/provenance sang update_validated, tương thích frozen.
- resolve_xhs_urls đối chiếu canonical ID; khác ID, path không liên quan, credentials và port lạ đã có regression tests.
- Probe khác note ID xác nhận không còn dùng token bài A cho canonical bài B.

## Findings còn lại
### F1 — P2: Tìm lại video làm mất token đã lưu
backend/services/search_service.py:325-326
Nhánh duplicate ghi đè existing.share_url bằng bất kỳ result.share_url truthy nào. Provider có thể trả canonical khi không lấy được token. Do đó tìm lại video đã có token sẽ thay link token bằng canonical.

Probe dùng SQLite in-memory và persist_results thật: lần đầu lưu URL ?xsec_token=SYNTHETIC, lần hai cùng ID chỉ có canonical. Sau lần hai, DB chỉ còn canonical.
Ảnh hưởng: làm giảm khả năng mở lại bài Xiaohongshu và quay lại advisory thiếu token; không khẳng định mọi canonical đều 404.

Sửa: chính sách merge riêng cho share URL. Giữ token cũ cùng ID khi incoming không có token hợp lệ; token mới cùng ID được ưu tiên. Nếu cần đánh dấu token hết hạn, phải dựa trên kết quả kiểm chứng riêng, không suy từ một lần crawl thiếu token.
Test: token→canonical giữ token; token→token thay mới; canonical→token nâng cấp; URL khác ID bị từ chối. Giữ nguyên trạng thái saved/favorite và liên kết JobVideo.

### F2 — P3: Nhận diện token bằng substring tạo false positive
backend/providers/xiaohongshu/parser.py:135-145; frontend/features/video/video-card.tsx:55,70
Hàm kiểm tra chuỗi xsec_token= thay vì parse query. Probe cho thấy cả ?xsec_token=, #xsec_token=FAKE và ?not_xsec_token=FAKE đều được giữ như token URL.
Frontend cùng kiểu kiểm tra nên không hiện advisory mặc dù query không chứa token có giá trị.

Sửa: backend parse_qs(urlsplit(url).query), frontend URL.searchParams.getAll('xsec_token'); yêu cầu giá trị không rỗng sau trim, chính sách rõ ràng cho duplicate parameter. Dùng helper chung trong từng ngôn ngữ cho resolver, merge và UI. Token đúng tên trong query mới được nhận diện; không xem fragment/path/tên tham số gần giống là token.
Test các case trên, token query hợp lệ và giá trị encode.

## Kiểm thử vòng này
- Pytest: 100 passed, 2 dependency deprecation warnings, 41.42s.
- Ruff: passed.
- Mypy: passed, 36 source files.
- TypeScript: passed, chạy --incremental false.
- ESLint: 0 errors, 2 cảnh báo img cũ.
- Probe độc lập: hai lỗi vòng 9 đã sửa; F1 và F2 tái hiện.
- Không chạy lại production build/E2E ở vòng này vì frontend không đổi so với vòng 9. Kết quả 10/10 E2E và build vòng 9 là bằng chứng lịch sử, không phải lần chạy mới.
- Không gọi Gemini hoặc crawler thật; probe ghi SQLite in-memory, không ghi database người dùng.

## Tình trạng phase
Phase 0–4 có mã nguồn: nền tảng app, mock, AI expansion, Xiaohongshu, Douyin. Vòng này là ổn định hóa hợp đồng dữ liệu và URL.
Phase 5 tìm thật nhiều nền tảng chưa triển khai: backend/api/search.py chỉ nhận một platform cho real mode và UI chọn từng mode.
docs/ROADMAP.md có đoạn cũ nói Douyin chưa có, trái checklist Phase 4 đã hoàn thành. docs/PHASE4.md nói đọc detail đồng thời, nhưng provider hiện dùng vòng lặp tuần tự và card có title không được enrich detail. Cần cập nhật tài liệu đúng runtime.
Chưa có bằng chứng live smoke mới cho bản hiện tại; không kết luận độ ổn định trên website thật từ unit/fixture tests.

## Kế hoạch bàn giao
### Phase 4.5 — Chốt độ tin cậy dữ liệu
1. Sửa F1 bằng merge share URL theo token và note ID.
2. Sửa F2 bằng parser query nhất quán backend/frontend.
3. Bổ sung regression test qua persist_results + DB + API đọc lại + thao tác mở link UI.
4. Cập nhật ROADMAP/PHASE4 và ghi rõ kiểm thử fixture so với live.
5. Chạy Ruff, Mypy, pytest, TypeScript, lint, build rồi E2E tuần tự.
6. Live smoke có kiểm soát: AI off, limit 5 mỗi nền tảng, kiểm tra đúng video/ID/title/link, saved/reload, tìm lại không mất token, hủy và resume đăng nhập. Khi CAPTCHA xuất hiện thì dừng chờ người dùng xử lý. Không cam kết đủ 5 nếu nền tảng chặn.

Điều kiện nghiệm thu: F1/F2 không tái hiện; gate xanh; có biên bản live ghi số kết quả, trạng thái và giới hạn thực tế, không ghi token/cookie.

### Phase 5 — Tìm nhiều nền tảng
Chỉ bắt đầu sau Phase 4.5:
- Định nghĩa mode real thống nhất với danh sách platforms; từ chối mode/platform mâu thuẫn.
- Budget tổng và budget mỗi nền tảng, giới hạn truy vấn; không nhân đôi limit vô ý.
- Tiến độ/trạng thái riêng XHS và Douyin; một nền tảng chờ login không chặn nền tảng còn lại.
- Kết quả partial được lưu; retry/resume idempotent, không lặp JobVideo; cancel giải phóng browser lock/waiter.
- Mặc định concurrency thấp, mỗi profile một tác vụ; kiểm thử một nền tảng timeout/CAPTCHA và nền tảng kia hoàn thành.
- UI hiển thị nguồn và tiến độ riêng, tổng kết thành công một phần; test AI off không gọi AI.

Ưu tiên tiếp theo là Phase 4.5, sau đó Phase 5; chưa cần embeddings/transcript/OCR.

