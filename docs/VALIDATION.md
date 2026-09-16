# Kiểm chứng Phase 0/1 — 2026-09-04

Bản kiểm chứng: C:\Users\admin\Projects\ai-video-research-tool.
Python 3.12.14, Node 26.4.0, Windows, Next 16.3.4, Chromium qua Playwright.

| Gate | Kết quả |
|---|---|
| Ruff backend/tests | PASS |
| mypy backend | PASS: 18 modules, Pydantic plugin |
| pytest | PASS: 13 tests |
| ESLint frontend | PASS |
| TypeScript frontend | PASS |
| Next.js production build | PASS: 8 static pages |
| setup.ps1 qua Windows PowerShell 5 | PASS: dependencies, Chromium, npm ci, build |
| start.ps1 | PASS: loopback 3000/8000, health sẵn sàng |
| Playwright E2E | PASS: 2 tests |
| Restart persistence | PASS: cùng tập ID video đã lưu trước/sau restart |
| Stop isolation | PASS: tiến trình Node sentinel ngoài hồ sơ dự án vẫn chạy |
| Browser runtime/visual QA | PASS: Search và Library, 0 pageerror |

Ảnh: logs/ui-search.png, logs/ui-library.png (gitignored).

## Những lỗi đã sửa qua kiểm thử

- JSX malformed option; React state synchronization và request timeout/race handling.
- Mypy tích hợp plugin Pydantic, import sorting, định dạng nguồn.
- PowerShell 5 truyền dấu nháy Node; chọn Python đủ phiên bản.
- JSON date parsing/array differences giữa PowerShell 5 và 7 trong Stop.
- Xác nhận process exit trên Windows; giữ hồ sơ nếu không xác minh/dừng được.
- Kịch bản E2E chọn video khác khi Skip, không ghi đè video đã Save rồi kỳ vọng vẫn Saved.

## Giới hạn

Phase 0/1 chưa test AI; Phase 2 đã kiểm chứng Gemini như mục bên dưới. Chưa test platform thật; mock URL/thumbnail không đại diện video thật.
Pytest có 2 deprecation warnings từ Starlette/httpx/anyio, không ảnh hưởng kết quả.
Npm báo unrs-resolver postinstall chưa được allow; không mở quyền script này, lint/build vẫn pass.
Lần cài ở OneDrive bị Controlled Folder Access chặn; bản Projects hoạt động mà không đổi cấu hình Defender.


## Phase 2 — 2026-09-04

- Ruff backend/tests, mypy 24 modules, pytest **29/29** pass; ESLint, TypeScript, Next production build pass.
- Chromium E2E **3/3** pass. Fixture preview được route intercept trong test; production gọi Gemini thật. Các test backend AI dùng fake provider/httpx.MockTransport, không tiêu quota người dùng.
- Live adapter: “dụng cụ bóc sticker” → “贴纸去除工具”, source=gemini, JSON validated. Kết quả trong logs/ai-live-check.json; không có khóa.
- Live UI/proxy: “unbox đồ cute” → “可爱物品开箱”. Preview hiển thị, chọn/sửa được. Desktop 1440px và mobile 390px không tràn ngang; pageerror=0. Cài đặt password input trống, GET settings không chứa khóa.
- Ảnh visual QA: logs/ui-ai-preview.png, logs/ui-ai-mobile.png, logs/ui-ai-settings.png.
- Auto AI search tái sử dụng cache live preview, lưu source/expansion/queries, completed. Không gọi thêm Gemini cho bộ query mỗi platform.
- Lần smoke đầu đã gửi request nhưng console CP1252 không in được Unicode; các lần xác minh sau dùng UTF-8/JSON ASCII. Lỗi nằm ở console kiểm thử, không đưa dữ liệu giả vào ứng dụng.
- Billing: người dùng xác nhận Free Tier/no Billing; không bật Billing hoặc mua dịch vụ. Khả năng gọi API được xác minh, trạng thái billing không được kiểm toán độc lập.
- Các trường hợp được kiểm thử: schema/budget/original mismatch, cache copy, quota cooldown/no retry, tối đa 3 attempts, auth/HTTP error không lộ key, hủy trong AI, manual bỏ qua AI, fallback vẫn tìm, settings write-only/preserve key, job cũ không có plan.

- Final restart PASS: cung tap ID video da luu, bo query/source/expansion AI con nguyen; AI config san sang, GET settings khong tra khoa.


## MediaCrawler import bridge — 2026-09-04

- Ruff pass; mypy 28 modules pass; pytest **41/41** pass.
- ESLint pass, TypeScript/production build pass. Chromium E2E **4/4** pass (3 regression + 1 import UI).
- Parser tests: JSON/JSONL/BOM, XHS/Douyin mapping, second/millisecond timestamp, missing metrics NULL, bài ảnh/comments/unknown type, ID precision, file limits, token/cookie fields discard, unsafe URL discard.
- SQLite tạm: preview không ghi DB, commit giữ provenance, nhập lại loại trùng, giữ saved/status/metadata; API nguồn nhập và lỗi không echo dữ liệu file.
- Browser importer E2E dùng route fixture và không nhập record giả vào DB người dùng. Test preview riêng nối backend thật với nội dung ghi KIỂM THỬ, trước/sau stats giống nhau; không bấm commit.
- Visual QA desktop 1440/mobile 390: pageerror=0, không tràn ngang. Ảnh logs/ui-mediacrawler-preview-test.png và logs/ui-mediacrawler-mobile-test.png chứa dữ liệu preview kiểm thử.
- Chưa nhận file export thật từ người dùng; chưa kiểm chứng crawler live/đăng nhập.
- Pytest ban đầu gặp giới hạn Windows do ID của parameter chứa payload UTF-8 rất dài; đổi sang tên test ngắn. Không phải lỗi importer. Hai deprecation warnings Starlette/httpx/anyio còn như trước.


## Phase 3 (Xiaohongshu Real Provider) — 2026-09-05

- **Ruff:** All checks passed (0 errors).
- **Mypy:** Success: no issues found in 32 source files.
- **Pytest:** **55/55** passed (100%), bao gồm các test unit mới:
  - `test_note_url_supports_rednote_and_all_routes`: URL parser chuẩn hóa cả rednote.com và xiaohongshu.com, các định dạng explore/search_result/discovery.
  - `test_parse_detail_extracts_author_engagement_and_hashtags`: Trích xuất đầy đủ author (`author_name`, `author_id`, `author_url`), interaction stats (`like_count`, `favorite_count`, `comment_count` hỗ trợ `万`, `w`, `k`), `hashtags`, `duration_seconds`.
  - `test_classify_page_url_error_codes`: Phân loại mã lỗi 300012 (`restricted`), 300031 (`not_found`), login-modal (`login_required`).
  - `test_browser_status_does_not_report_its_own_lock_as_busy`: Tránh báo bận giả lập khi chính tiến trình sở hữu context.
- **Frontend QA:**
  - `npm run lint`: 0 errors, 0 warnings.
  - `npm run typecheck`: 0 errors.
  - `npm run build`: Production build thành công 9/9 routes.
  - `npm run test:e2e`: 5/5 passed (Search, Save, Library, MediaCrawler import, Browser controls).
- **Chẩn đoán kỹ thuật Live:**
  - Phát hiện phiên đăng nhập của người dùng nằm trên domain quốc tế `.rednote.com` (có `id_token` 136 bytes), trong khi `.xiaohongshu.com` chưa có session dẫn đến `login-modal`.
  - `BrowserService.detect_base_url()` tự động chọn domain chính xác theo `id_token`.
  - Khắc phục lỗi 404 (`error_code=300031`) khi mở chi tiết bài viết bằng cách lưu `share_url` giữ nguyên `xsec_token`.
- **Live Search & SQLite Persistence Verification:**
  - Chạy `SearchService` thật với query tiếng Trung `文具开箱`, limit = 5, mode = `xiaohongshu`, `is_mock=0`.
  - Job ID: `3d00695a-dbbf-4cfd-a868-f24ee63ae135` hoàn thành thành công và lưu 5 video thật vào SQLite `data/app.db`:
    1. `1353ba00-c34b-4348-984f-129352b2b267`: `✏️拥有了实体emoji铅笔！好萌一只蓝白波点～` - Tác giả: 汪汪历险记, Thời lượng: 15.034s, Likes: 19, Favorites: 7952, Comments: 182, Trạng thái: `saved`.
    2. `31db7075-eea9-4ab0-9c0e-8533f56cc374`: `😽我宣布最适合吃谷人的痛笔出现了✏✨` - Tác giả: 大晗同学, Thời lượng: 48.567s, Likes: 74, Favorites: 2116, Comments: 123.
    3. `415268d1-f0b4-4374-b30c-8cc55102811d`: `草纸推荐` - Tác giả: 风不息, Thời lượng: 32.234s, Likes: 1286, Favorites: 13500, Comments: 244.
    4. `7918087d-2a97-48da-99fe-c34911ebbb89`: `睡前读物开箱！有些文创确实🔥的有道理……` - Tác giả: 萌好记, Thời lượng: 294.0s, Likes: 1, Favorites: 11, Comments: 31.
    5. `893c7a93-a68b-46be-921c-8f18c9191068`: `才0.5💰一卷吗...🥹` - Tác giả: 浓浓, Thời lượng: 17.585s, Likes: 34, Favorites: 2957, Comments: 366.
  - Restart & Persistence: Kiểm tra trực tiếp file SQLite `data/app.db` độc lập, cả 5 video thật và trạng thái `saved` được bảo toàn nguyên vẹn.

