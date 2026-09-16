# Roadmap

- [x] Phase 0 — Foundation, Windows setup/start/stop, SQLite, health, config, logging.
- [x] Phase 1 — Mock search, dedup, ranking, polling, filter/sort, save/skip, library, E2E.
- [x] Phase 2 — AIProvider, JSON query expansion, validation + 2 retries, original-query fallback.
- [x] Phase 3 — RED browser provider; manual login/CAPTCHA; chứng minh bằng kết quả thực tế.
- [x] Phase 4 — Douyin provider theo cùng contract.
- [x] Phase 4.5 — Chốt độ tin cậy dữ liệu & Token URL (bất biến model, chống mất token khi tìm trùng, query parser chuẩn xác).
- [ ] Phase 5 — Search đa nền tảng thật, budget/concurrency thấp.
- [ ] Phase 6 — Find Similar và relation source video/job.
- [ ] Phase 7 — Embeddings, semantic search.
- [ ] Phase 8 — Transcript, OCR, visual understanding (ngoài MVP).
- [ ] Phase 9 — Trends, scheduled research (ngoài MVP).

## Giới hạn có chủ đích

- Phase 2 dùng Gemini thật để tạo từ khóa; Phase 3 đã hoàn thành tìm kiếm thật trên Xiaohongshu (RED); Phase 4 đã hoàn thành tìm kiếm thật trên Douyin.
- Điểm relevance là lexical/token overlap, không gọi là semantic similarity.
- Tìm thật hiện hỗ trợ cả XHS và Douyin qua Chrome profile riêng biệt (chạy đơn nền tảng ở Phase 3 & 4; tìm đa nền tảng đồng thời triển khai ở Phase 5).
- Collections/export vẫn chưa triển khai.
- MVP Phase 3 & 4 đã hoàn thành với 2 real provider được kiểm chứng bằng video thật lưu vào SQLite.

## Trạng thái triển khai hiện tại

Phase 0/1 đã nghiệm thu ngày 2026-09-04 tại bản chạy `C:\Users\admin\Projects\ai-video-research-tool`.
Bản OneDrive là snapshot ban đầu, không phải bản đang chạy/phát triển.

- Backend: Ruff pass, mypy pass (18 modules), pytest 13/13 pass.
- Frontend: ESLint pass, TypeScript pass, production build pass.
- Windows setup.ps1, start.ps1, stop.ps1 chạy thực tế thành công (PowerShell 5).
- Chromium E2E: 2/2 pass; Search → Save → Library → reload, filters/skip, cancel.
- Stop/start giữ nguyên video đã lưu; tiến trình Node kiểm chứng ngoài dự án vẫn sống.
- Browser JavaScript errors: 0 trong lượt visual QA Search/Library.
- Dependency versions đã khóa trong requirements.lock.txt và package-lock.json.

## Nghiệm thu Phase 2 — 2026-09-04

- Gemini REST adapter + AIProvider interface, JSON schema, validation, tối đa 2 retry, quota cooldown và fallback.
- Preview/edit/select tối đa 10 query; auto AI tùy chọn; lưu bộ query theo job và giữ lịch sử cũ.
- Cài đặt provider/model, khóa write-only và test kết nối; .env được thay thế nguyên khối, không ghi lộ secret.
- Pytest 29/29; Ruff, mypy (24 modules), ESLint, TypeScript, production build đều pass.
- Chromium E2E 3/3: tìm/lưu/lọc/hủy và preview/edit/manual query persistence. Các test tự động dùng fake transport/route; không gọi AI thật.
- Live Gemini kiểm chứng từ adapter và giao diện: “unbox đồ cute” → “可爱物品开箱”. Không fake dữ liệu AI trong production.
- Visual QA desktop/mobile: không tràn ngang, không pageerror; Cài đặt không điền ngược khóa.
- Auto-AI search dùng cache đã kiểm chứng và kết thúc thành công. Chi tiết xem VALIDATION.md.

## Nghiệm thu Phase 3 — 2026-09-05

- Xiaohongshu browser provider qua Chrome profile riêng (`data/browser/xiaohongshu`).
- Chẩn đoán và xử lý dứt điểm vòng lặp đăng nhập thông qua cơ chế tự động nhận diện domain (`rednote.com` vs `xiaohongshu.com`) dựa trên cookie xác thực `id_token`.
- Giải quyết lỗi 404 bài viết bằng cơ chế lưu trữ kép `canonical_url` (chuẩn hoá, deduplication) và `share_url` (giữ `xsec_token` phục vụ mở trực tiếp từ UI).
- Trích xuất đầy đủ DOM video thật: author (`author_name`, `author_id`, `author_url`), interaction metrics (`like_count`, `favorite_count`, `comment_count`), `hashtags`, `duration_seconds`.
- Nghiệm thu thực tế: Thu thập và lưu thành công 5 video thật vào SQLite `data/app.db` (`is_mock=0`). Thao tác Lưu/Bỏ qua và restart kiểm chứng dữ liệu bền vững.
- Kiểm thử tự động: Pytest 55/55 pass, mypy (32 modules) pass, ruff pass, ESLint & TypeScript pass, production build 9 static routes pass, Chromium E2E 5/5 pass.

## Nghiệm thu Phase 4 — 2026-09-05

- Douyin browser provider qua Chrome profile riêng (`data/browser/douyin`).
- Phát hiện CAPTCHA thanh trượt (`验证码中间页`) và Login, trả về đúng trạng thái `verification_required` / `login_required`.
- Trích xuất đầy đủ video URL, author, interactions.
- UI điều khiển Douyin riêng biệt, tích hợp vào Workspace.
- 100-video cap enforced on API and UI side.

## Nghiệm thu Phase 4.5 & 4.5b (Round 12) — 2026-09-07

- **Bất biến hóa model dữ liệu (`frozen=True`):** `VideoResult` và `VideoOut` hoàn toàn bất biến, ngăn chặn việc gán sai làm hỏng dữ liệu trong bộ nhớ. Mọi thao tác cập nhật đều qua `update_validated`.
- **Hệ thống chính sách danh tính hợp nhất (`backend/services/identity_policy.py`):**
  - **Xử lý liên kết ngắn (`xhslink.com` - F1):** Phân biệt rạch ròi giữa verified note URL và unverified short URL. Liên kết ngắn chưa qua phân giải không bao giờ được ghi đè lên URL đã xác minh có token hoặc URL canonical. Tham số token giả trên short link không được nâng cấp thành verified token. Hỗ trợ resolve async/sync theo redirect tối đa 3 hops, xác minh protocol HTTPS, domain tin cậy và đối chiếu note ID đích.
  - **Ngăn chặn triệt để mâu thuẫn ID và URL canonical (F2):** `VideoResult` và `validate_result_identity` kiểm tra tính nhất quán giữa `platform_video_id` và URL canonical; từ chối mọi trường hợp mâu thuẫn ID (như ID B với URL A).
  - **Hàm `merge_share_url` an toàn tuyệt đối:** Trả về `None` khi có mâu thuẫn danh tính, không âm thầm tự tạo URL từ ID sai lệch. Khi thiếu ID, tự động suy luận ID chính xác từ URL canonical.
  - **Cách ly ứng viên mâu thuẫn (`persist_results`):** Khử trùng và đưa vào quarantine các ứng viên mâu thuẫn ID trước khi vào `find_existing` và lưu trữ; tuyệt đối không tạo bản ghi rác trong `Video` hay liên kết `JobVideo`.
  - **Khử trùng dedup (`find_existing`):** Ngăn chặn dedup ghi đè hoặc hợp nhất sai chéo giữa các video khác nhau khi có mâu thuẫn `platform_video_id`.
- **Phân tích token chuẩn xác:** Loại bỏ substring check lỏng lẻo; chuyển sang phân tích chính thức query parameter `xsec_token` trên cả backend (`parse_qs`) và frontend (`searchParams.getAll`), loại bỏ hoàn toàn các trường hợp giả mạo (`?xsec_token=`, `#xsec_token=FAKE`, `?not_xsec_token=FAKE`).
- **Trạng thái kiểm thử:** 
  - Backend: 130/130 pytest passed (100%), Ruff passed, Mypy passed trên 45 source files.
  - Frontend: TypeScript passed, ESLint passed (0 errors, 2 warnings `<img>` cũ), Next.js production build 9 routes passed.
  - E2E: 18/18 Playwright E2E passed (chạy tuần tự: downloads, imports, search, xiaohongshu).
  - Nghiệm thu live crawler: Chờ tài khoản thật; fixture và automated tests được phân biệt rõ ràng, không coi fixture là thành công crawler thực tế.

## Bước tiếp theo

Phase 5: Tìm kiếm đồng thời đa nền tảng (cả XHS và Douyin), quản lý budget và concurrency limit.

## Bổ sung theo tham khảo MediaCrawler

- [x] Đọc source và giấy phép tại commit d6f7c5bb906b6dac40ddf343ef9e26438a3de092; ghi phân tích trong MEDIACRAWLER.md.
- [x] Adapter độc lập cho content JSON/JSONL XHS/Douyin; preview/commit, validation, dedup, provenance, giữ trạng thái thư viện.
- [ ] Nghiệm thu bằng file export thật do người dùng cung cấp.

