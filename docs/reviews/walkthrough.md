# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py --live`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **PASS (0 lỗi)**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.36s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.11s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [0.72s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [5.69s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [2.21s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [15.13s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **441/441 PASS** | 441 passed, 0 failed, 3 skipped [143.48s] |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py ...` | **PASS** | 60 truy vấn domain + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) [2.41s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **25/25 PASS** | 25 ca kiểm thử E2E trên 7 tập tin spec [82.71s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200: verification_required, XHS: HTTP 200: page_available). Retrieval: Douyin BLOCKED (CHALLENGE_REQUIRED), XHS BLOCKED (LOGIN_REQUIRED) [107.28s] |

**Phán quyết Cổng Phát hành (Verdict)**:  
`APPROVED (WORKSTATION READY) - LIVE RETRIEVAL BLOCKED BY AUTH` (Tổng thời gian: 365.21s)  
*Chi tiết kiểm chứng lưu tại tệp versioned:* [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json).

---

## 2. Tiêu Chuẩn Thực Thi Lệnh & Chế Độ Nghiêm Ngặt (Strict Gate)

| Mục Đích Kiểm Thử | Lệnh Tiêu Chuẩn | Hành Vi & Phán Quyết | Mã Thoát (Exit Code) Thực Tế |
| :--- | :--- | :--- | :---: |
| **Nghiệm thu toàn bộ 11 Stage (Vận hành máy trạm)** | `python scripts/release_gate.py --live` | Chạy toàn bộ 11 stage. Ở Stage 11, kiểm tra Chromium thật mở được Douyin và XHS (HTTP 200). Khi gặp CAPTCHA/Login, hệ thống ghi nhận đúng trạng thái và kết luận `APPROVED (WORKSTATION READY)`. | `0` |
| **Kiểm thử nghiêm ngặt thu thập thật (Strict Gate)** | `python scripts/release_gate.py --live --require-live` | Bắt buộc cả Douyin và XHS phải trả về `>= 1 video thật`. Do phiên live hiện tại chưa tự giải CAPTCHA / đăng nhập trên trạm làm việc, cổng phát hành từ chối và thoát mã lỗi 2 (fail-closed). | `2` (fail-closed, không báo FULL GREEN ảo) |
| **Kiểm thử hồi quy backend** | `pytest -q` | Chạy toàn bộ 441 bài kiểm thử tự động, 3 bài test live crawl được bỏ qua an toàn khi chưa bật `LIVE_CRAWL_ENABLED=1`. | `0` |
| **Chạy cổng phát hành offline (10 Stage)** | `python scripts/release_gate.py` | Chạy đầy đủ từ Stage 1 đến Stage 10 (bỏ qua Stage 11 Live Crawl). | `0` |

---

## 3. Phân Định Minh Bạch Các Tầng Kiểm Thử

### A. Kiểm Thử Offline (Offline Verification)
- **Tổng số ca kiểm thử**: 441 passed, 3 skipped, 0 failed trong test suite backend.
- **Tích hợp Pipeline thật** ([`tests/test_search_service_resume.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_search_service_resume.py)):
  - Kiểm thử đầy đủ luồng thực thi `SearchService.pipeline()` với SQLite DB thật trên cả 2 thứ tự nền tảng (Douyin $\to$ Xiaohongshu và Xiaohongshu $\to$ Douyin).
  - Khi nền tảng thứ hai bị chặn (`UserActionRequired`), job lưu trữ chính xác `active_platform`, `waiting_reason`, đồng thời kết quả thu thập của nền tảng thứ nhất được bảo toàn đầy đủ trong bảng `Video` và `JobVideo`.
  - Kiểm thử cơ chế loại bỏ trùng lặp (`deduplication`): khi provider trả lại kết quả một phần trước khi chặn và trả lại toàn bộ sau khi tiếp tục, hệ thống không tạo thêm bản ghi `JobVideo` trùng lặp nào.
  - Kiểm thử timeout thao tác thủ công: khi người dùng không hoàn thành xác minh/đăng nhập trong thời hạn cho phép, hệ thống đưa ra thông báo rõ ràng `"Đã hết thời gian chờ đăng nhập/xác minh trên <nền tảng>. Vui lòng thử lại khi đã sẵn sàng thao tác"`, không bị báo nhầm là lỗi tìm kiếm chung.
  - Kiểm thử endpoint `POST /api/search/jobs/{job_id}/resume`: trả về đúng **HTTP 409 Conflict** kèm thông điệp tiếng Việt nếu trình duyệt chưa hoàn thành thao tác, và trả về **HTTP 200 OK** khi đã xác thực.

### B. Kiểm Thử Giao Diện & E2E (UI & Frontend Verification)
- **Playwright E2E**: 25/25 bài test pass trên 7 file spec (`frontend/tests/`).
- **Giao diện điều khiển trình duyệt** ([`frontend/features/search/search-workspace.tsx`](file:///C:/Users/admin/Projects/ai-video-research-tool/frontend/features/search/search-workspace.tsx)):
  - `BrowserControls` và banner thông báo sử dụng trường `active_platform` từ job, hướng người dùng trực tiếp tới đúng cửa sổ trình duyệt của nền tảng đang bị kẹt (thay vì cố định `platforms[0]`).
  - Lỗi HTTP 409 khi tiếp tục thủ công thất bại được hiển thị trực quan qua `ErrorBanner`.
- **Chuẩn hóa thông điệp**:
  - Gỡ bỏ hoàn toàn các phát biểu thiếu thực tế: `"2 giây"`, `"không cần giải đố"`, `"lưu vĩnh viễn"`.
  - Thông báo trung thực: Phiên được lưu trữ an toàn trong Chromium profile cục bộ của ứng dụng, phiên có thể hết hạn theo chính sách bảo mật nền tảng và người dùng tự quét mã QR / giải CAPTCHA trực tiếp.

### C. Live Network Probe
- Cả Douyin và Xiaohongshu đều được thăm dò trực tiếp qua Chromium headless thật:
  - Douyin: HTTP 200, phản hồi `verification_required` (thanh trượt CAPTCHA).
  - Xiaohongshu: HTTP 200, phản hồi `page_available` (phiên khách vãng lai).
- Kết nối mạng đạt HTTP 200, không bị lỗi DNS hay timeout kết nối.

### D. Live Retrieval Từng Nền Tảng (Trạng Thái Thực Tế)
- **Douyin**: `BLOCKED (CHALLENGE_REQUIRED)`. Douyin kích hoạt cơ chế bảo vệ chống bot với thanh trượt xác minh bảo mật. Do công cụ tuân thủ nguyên tắc không can thiệp giải mã CAPTCHA tự động, crawler dừng lại và yêu cầu người dùng tự giải quyết.
- **Xiaohongshu**: `BLOCKED (LOGIN_REQUIRED)`. Xiaohongshu yêu cầu phiên đăng nhập tài khoản người dùng để truy cập đầy đủ kết quả tìm kiếm ghi chú/video.
- **Kết luận**: Hiện tại Stage 11 có `passed: false`. Tool **chưa có bằng chứng thu thập được video thật** khi chưa có phiên người dùng tự xác thực. Trạng thái hệ thống là **WORKSTATION READY**, tuyệt đối không công bố `FULL GREEN`.

### E. Retrieval Precision Benchmark (Đánh Giá Độ Chính Xác)
Bộ kiểm thử độ chính xác ([`tests/test_query_precision_evaluation.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_query_precision_evaluation.py)) đã được tách bạch rõ ràng thành 2 phân hệ:
1. **Ranking Unit/Fixture Benchmark (15 truy vấn tiếng Việt thực tế)**:
   - Đánh giá khả năng phân loại của bộ xếp hạng trên các cặp mẫu liên quan và mẫu gây nhiễu dựng sẵn.
   - Báo cáo rõ ràng dưới dạng `Precision@k` theo số mẫu ứng viên thực tế ($k=3 \dots 5$) và số lượng mẫu, không gọi nhầm là Precision@10.
   - Truy vấn `"nhíp gắp sticker"`: bắt buộc giữ nguyên từ khóa cốt lõi `镊子` (nhíp) và `贴纸` (sticker); loại bỏ triệt để các ghi chú chỉ bán sticker thông thường mà không có nhíp.
2. **Crawled Dataset Retrieval Precision Benchmark (Dữ liệu crawl thực tế)**:
   - Đánh giá trên 20 mẫu crawl thật có nguồn gốc, URL, timestamp và nhãn người duyệt từ [`data/benchmarks/crawled_benchmark_samples.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/data/benchmarks/crawled_benchmark_samples.json).
   - Đạt **Precision = 1.00** và **Accuracy = 1.00** trên nền tảng Douyin, phân loại chính xác 12 mẫu liên quan và loại bỏ 8 mẫu gây nhiễu (kèm lý do ghi nhận đầy đủ).
3. **Live Retrieval Precision**:
   - Trạng thái: `PENDING LIVE CRAWL / BLOCKED BY AUTH`. Khi chạy kiểm thử mà chưa có dữ liệu live thật, hệ thống báo pending trung thực, không bao giờ tự tạo kết quả giả.

---

## 4. Hướng Dẫn Vận Hành & Nghiệm Thu Live Thực Tế

Để hoàn tất nghiệm thu Live Retrieval và đạt phán quyết `APPROVED (FULL GREEN)`:

1. **Bước 1: Tự hoàn thành xác minh trên Chromium của tool**:
   - Mở giao diện công cụ tại `http://localhost:3000/settings`.
   - Bấm **Mở cửa sổ Douyin** $\to$ giải thanh trượt CAPTCHA trực tiếp trên cửa sổ trình duyệt.
   - Bấm **Mở cửa sổ Xiaohongshu** $\to$ quét mã QR đăng nhập tài khoản.
   - Phiên làm việc sẽ được lưu tự động vào profile Chromium cục bộ tại `data/browser/douyin` và `data/browser/xiaohongshu`.
2. **Bước 2: Thực hiện kiểm thử Live Search**:
   - Chạy tìm kiếm với các từ khóa đại diện: `"nhíp gắp sticker"`, `"decor bàn học"`.
   - Kiểm tra các video thu thập được có `media_type == "video"`, URL hợp lệ, `is_mock == false`.
3. **Bước 3: Chạy Cổng Phát Hành Nghiêm Ngặt**:
   - Chạy lệnh: `python scripts/release_gate.py --live --require-live`
   - Chỉ khi cả hai nền tảng thu thập thành công video thật, lệnh sẽ trả về **exit code 0** và phán quyết chuyển sang `APPROVED (FULL GREEN)`.
