# Review Antigravity vòng 11 — kế hoạch phase tiếp
Ngày kiểm tra: 2026-09-07
Codebase: C:/Users/admin/Projects/ai-video-research-tool

## Đánh giá tiến độ
Đã triển khai các thay đổi chính Phase 4.5: model frozen và validated updates; merge_share_url bảo toàn token trong trường hợp thông thường; parser query token backend/frontend; regression test persist_results bảo toàn favorite/JobVideo; E2E token rỗng/giả; tài liệu đã cập nhật.
Hai lỗi vòng 10 (token bị thay bằng canonical và substring token false positive) được sửa cho các trường hợp đã báo. Backend có 103 test so với 100 vòng trước, E2E tập trung có 11 ca so với 10.
Phase 5 chưa triển khai: backend/api/search.py vẫn chỉ cho real platforms bằng [xiaohongshu] hoặc [douyin]. ROADMAP cũng đánh dấu Phase 5 chưa làm.

## F1 — P2: merge_share_url trả lại URL đã bị xác định sai ID
File backend/services/search_service.py, hàm merge_share_url (khoảng dòng 29-76), nhánh cuối Xiaohongshu:
return incoming_share_url or existing_share_url

Hàm tính incoming_valid/existing_valid theo note ID, nhưng nếu cả hai không hợp lệ thì lại trả chính incoming sai.
Tái hiện: record A có share_url=None, incoming có url canonical A và share_url token của B. VideoResult hiện chấp nhận vì schema chỉ kiểm tra hostname/platform. persist_results tìm đúng record A rồi lưu share_url B.
Probe SQLite in-memory qua service thật cho kết quả:
DB_ID aaaaaaaaaaaaaaaaaaaaaaaa
DB_SHARE_URL https://www.xiaohongshu.com/explore/bbbbbbbbbbbbbbbbbbbbbbbb?xsec_token=SYNTHETIC

Một nhánh nữa: nếu record platform_video_id=None, mọi incoming note ID đều được coi hợp lệ, dù existing URL là A và incoming là B.
Đây là lỗi hợp đồng merge được tái hiện bằng dữ liệu tổng hợp; chưa có bằng chứng crawler hiện tại tự tạo trường hợp này ở live.

Sửa:
- Không trả incoming/existing đã thất bại kiểm tra.
- Truyền canonical URL của record vào merge; lấy expected ID từ platform_video_id hoặc canonical URL.
- Nếu không còn share URL hợp lệ: trả canonical đã xác thực của record, hoặc None.
- Kiểm tra tính nhất quán URL/note ID tại ranh giới lưu dữ liệu. Phân biệt short URL chưa resolve với URL note có ID rõ ràng; không tùy tiện bỏ hỗ trợ short link.
- Test None + wrong ID, cả hai URL sai, record thiếu ID nhưng có canonical A, token A→canonical A, token A→token A mới.
- Integration test qua persist_results và API đọc lại để khẳng định card/link đều thuộc A, giữ status và JobVideo.

## Khoảng trống nghiệm thu
ROADMAP đánh dấu Phase 4.5 hoàn tất dựa trên test tự động. Trong nội dung mới đã đọc chưa có biên bản live smoke mới cho phiên bản này: 5 video mỗi platform, mở đúng bài, tìm lại, save/reload, cancel/resume.
Không nên đồng nhất fixture E2E với thành công trên website thật. Tạm ghi “implementation và automated tests hoàn tất, live acceptance pending”.
Không cần phát sinh thêm lỗi giả để kéo dài review; F1 là finding mã nguồn mới đã xác nhận.

## Kiểm thử thực hiện vòng này
- Pytest: 103 passed, 2 dependency warnings, 42.18s.
- Ruff: passed.
- Mypy: passed, 36 source files.
- Production build Next.js: passed; TypeScript trong build passed; 9 routes.
- ESLint: 0 errors, 2 cảnh báo img cũ.
- Playwright tests/xiaohongshu.spec.ts: 11 passed, 7.3s, chạy sau khi build xong.
- Probe backend token rỗng/fragment/not_xsec_token: đều False.
- Probe merge và SQLite service: tái hiện F1.
Không chạy toàn bộ E2E có ghi database ứng dụng; chỉ chạy file tập trung. Không gọi Gemini/crawler thật, không ghi database người dùng. Không sửa source trong review.

## Phase kế tiếp
### Phase 4.5b — chốt merge và nghiệm thu
1. Sửa F1, bổ sung các regression tests nêu trên.
2. Chạy backend/static gates, build rồi E2E tuần tự.
3. Live smoke AI off, limit 5 mỗi nền tảng: kiểm tra ID/title/link; lưu và reload; tìm lại không mất token; hủy và resume. Khi login/CAPTCHA thì người dùng tự xử lý.
4. Lưu biên bản với số kết quả và trạng thái, không ghi token/cookie. Nếu bị hạn chế, ghi blocked/partial cùng nguyên nhân, không đánh dấu hoàn tất giả.

### Phase 5 — nhiều nền tảng thật
Sau khi Phase 4.5b đạt:
1. Contract real mode với platforms rõ ràng, từ chối mode/platform mâu thuẫn; tương thích request cũ.
2. Budget tổng và mỗi nền tảng; tổng unique results không vượt limit.
3. Trạng thái, deadline, resume riêng từng nền tảng; một nền tảng chờ login không chặn nền tảng kia.
4. Kết quả partial bền vững; retry/resume idempotent; cancel giải phóng lock/waiter/browser.
5. Concurrency mỗi profile tối đa 1; AI mặc định tắt; không mở rộng tự động số query.
6. UI tiến độ từng nguồn và kết quả thành công một phần.
7. Test độc lập XHS bị CAPTCHA/Douyin thành công và ngược lại; timeout, retry, cancel, restart, dedup và total budget.

Không cần triển khai embeddings/OCR/transcript trước các bước trên.

