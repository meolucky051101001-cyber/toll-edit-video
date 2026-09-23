# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **100% GREEN**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.9s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.11s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [0.44s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [9.26s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [4.81s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [26.32s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **416/416 PASS** | Toàn bộ 416 ca kiểm thử hồi quy đạt chuẩn (0 failed) [150.82s] |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **PASS** | 60 truy vấn chuẩn + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) [4.09s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **23/23 PASS** | 23 ca kiểm thử E2E trên 7 tập tin spec (bao gồm `health.spec.ts` kiểm chứng dữ liệu thật) [44.03s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200, XHS: HTTP 200). Retrieval: minh bạch trạng thái CAPTCHA/Login [105.15s] |

**Phán quyết Cổng Phát hành (Verdict)**:  
`APPROVED (WORKSTATION READY) - LIVE RETRIEVAL BLOCKED BY AUTH`  
*Chi tiết kiểm chứng lưu tại tệp versioned:* [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json).

---

## 2. Tiêu Chuẩn Thực Thi Lệnh (Standardized Command Execution)

Để loại bỏ hoàn toàn sự nhập nhằng giữa các chế độ kiểm thử, hệ thống áp dụng bộ lệnh tiêu chuẩn sau:

| Mục Đích Kiểm Thử | Lệnh Tiêu Chuẩn | Hành Vi & Phán Quyết | Mã Thoát (Exit Code) |
| :--- | :--- | :--- | :---: |
| **Nghiệm thu toàn bộ 11 Stage (Vận hành máy trạm)** | `python scripts/release_gate.py --live` | Chạy toàn bộ 11 stage. Ở Stage 11, kiểm tra Chromium thật mở được Douyin và XHS (HTTP 200). Nếu gặp CAPTCHA/Login, hệ thống ghi nhận đúng trạng thái và kết luận `APPROVED (WORKSTATION READY)`. | `0` (nếu các stage 1-10 pass và mạng reachable) |
| **Kiểm thử nghiêm ngặt không thử thách bảo mật (Strict CI)** | `python scripts/release_gate.py --live --require-live` | Chạy 11 stage nhưng **bắt buộc** cả Douyin và XHS phải trả về `>= 1 video thật`. Nếu bị chặn bởi CAPTCHA hoặc yêu cầu đăng nhập, cổng phát hành từ chối và thoát mã lỗi 2. | `2` (khi gặp auth/challenge) |
| **Kiểm thử bộ mẫu chuẩn offline** | `pytest tests/test_real_crawl_acceptance.py` | Chạy kiểm thử hợp đồng dữ liệu, bộ lọc và ranking trên tập dữ liệu đã thu thập trước đó (bỏ qua live network khi chưa set `LIVE_CRAWL_ENABLED=1`). | `0` |
| **Chạy cổng phát hành offline (10 Stage)** | `python scripts/release_gate.py` | Chạy đầy đủ từ Stage 1 đến Stage 10 (bỏ qua Stage 11 Live Crawl). | `0` |

---

## 3. Các Cải Tiến Kỹ Thuật Đã Thực Hiện

### A. Khắc Phục Lỗi Dùng Lại HTTP Status Cũ Trong Live Probe (`browser_service.py` & `douyin_browser_service.py`)
- **Vấn đề trước đây:** `ensure_open()` trả lại page đang mở mà không tải lại trang. `probe_live()` trước đó chỉ gửi request mới nếu `last_http_status` chưa có; nếu browser đã từng nhận HTTP 200 thì trong các lần probe sau sẽ tiếp tục báo 200 dù kết nối mạng thực tế đã bị đứt hoặc trả về 503. Đồng thời, việc điều hướng trên cùng `self.page` có thể làm gián đoạn tiến trình tìm kiếm hoặc phân trang đang chạy dở.
- **Giải pháp triệt để:**
  1. Trong `probe_live()`, khởi tạo một trang probe riêng biệt: `probe_page = await self.context.new_page()`.
  2. Gửi một request điều hướng mạng hoàn toàn mới: `resp = await probe_page.goto(target_url, wait_until="domcontentloaded", timeout=20000)`.
  3. Cập nhật trực tiếp `self.last_http_status = resp.status` từ phản hồi tươi mới của lần gọi đó (không bao giờ dùng lại trạng thái cũ).
  4. Kiểm tra phân tích trang trên `probe_page` (`inspect(probe_page)`), sau đó đóng `probe_page` ngay lập tức trong khối `finally` (`await probe_page.close()`).
  5. Tiến trình tìm kiếm trên `self.page` được bảo vệ 100% không bị can thiệp hay gián đoạn.
  6. Bổ sung bài kiểm thử đơn vị `test_browser_service_probe_fresh_request_not_stale_status` và `test_douyin_browser_service_probe_fresh_request_not_stale_status` mô phỏng probe lần 1 trả HTTP 200, probe lần 2 trả HTTP 503 để chứng minh hệ thống phát hiện chính xác lỗi 503 ngay lập tức.

### B. Bổ Sung Thao Tác Live Probe Trên Dashboard & Phân Định Trực Quan Trạng Thái (`frontend/app/health/page.tsx`)
- **Vấn đề trước đây:** Trang `/health` chỉ tải dữ liệu cơ bản `/system/release-readiness` (không có `?probe=true`); nút "Làm mới" cũng chỉ gọi lại endpoint cơ bản khiến dashboard chưa có thao tác vận hành kiểm tra mạng thật.
- **Giải pháp triệt để:**
  1. Thêm nút hành động chuyên dụng **"Kiểm tra kết nối live"** (`data-testid="run-live-probe-btn"`) cạnh nút "Làm mới", có trạng thái loading xoay spinner khi đang dò mạng.
  2. Nút kích hoạt gọi trực tiếp `/api/system/release-readiness?probe=true` với thời gian chờ (timeout) 35s.
  3. Khi có kết quả live probe, giao diện hiển thị badge `Dữ liệu Live Probe trực tiếp` và mở rộng bảng **"Trạng thái kết nối mạng thực tế & Xác thực người dùng (Live Reachability)"** (`data-testid="live-probe-breakdown"`).
  4. Phân định rạch ròi giữa 3 trạng thái:
     - **Đã kết nối (Reachable - HTTP 200)**: Thể hiện kết nối mạng tới nền tảng hoàn toàn thông suốt.
     - **Cần xác thực/Đăng nhập (Auth Challenge Required)**: Ghi nhận rõ ràng "CẦN XÁC MINH BẢO MẬT (CAPTCHA)" cho Douyin và "CẦN ĐĂNG NHẬP PHIÊN" cho Xiaohongshu kèm thông tin hướng dẫn vận hành.
     - **Lỗi kết nối mạng (Network Error)**: Ghi nhận lỗi HTTP 5xx, timeout hoặc exception nếu có.

### C. Cô Lập Hoàn Toàn Bài Test Fail-Closed Khỏi Douyin Thật (`tests/test_observability_and_release_gate.py`)
- **Vấn đề trước đây:** Bài test cũ chỉ mock `app.state.browser.probe_live` rồi gọi endpoint `?probe=true`. Do endpoint dùng `asyncio.gather` nên `douyin_browser.probe_live` còn lại vẫn chạy ra mạng Douyin thật, khiến unit test bị phụ thuộc mạng và profile máy trạm.
- **Giải pháp triệt để:**
  1. Mock đồng thời cả 2 provider: `app.state.douyin_browser.probe_live` và `app.state.browser.probe_live`.
  2. Kiểm thử kịch bản 1: Douyin thành công, XHS ném `RuntimeError` -> xác nhận fail-closed (`status == "BLOCKED"`, `is_ready == False`, `checks["live_providers"]["status"] == "FAIL"`).
  3. Kiểm thử kịch bản 2: Douyin ném `RuntimeError`, XHS thành công -> xác nhận fail-closed.
  4. Kiểm thử kịch bản 3: Cả 2 thành công -> xác nhận `PASS` và `status == "READY"`.
  5. Toàn bộ 8 bài test trong suite chạy hoàn toàn độc lập với mạng thật chỉ trong 0.27 giây.

### D. Nâng Cấp E2E Health Kiểm Chứng Dữ Liệu Readiness Thật & Thao Tác Live Probe (`frontend/tests/health.spec.ts`)
- **Vấn đề trước đây:** Bài test E2E cũ chỉ kiểm tra tiêu đề và khung giao diện, không chờ hay xác nhận thẻ `readiness-check-*` xuất hiện trước khi kiểm tra lỗi console.
- **Giải pháp triệt để:**
  1. `test("waits for readiness response and renders check cards and scale telemetry")`: Chờ tường minh `page.waitForResponse` cho `/system/release-readiness`, xác nhận thẻ `data-testid="readiness-check-database"` hiển thị cùng trạng thái `database (PASS)`, xác nhận các thẻ `worker_pool`, `providers`, bấm nút "Làm mới", chờ phản hồi mới và kiểm chứng không có ngoại lệ JavaScript nào.
  2. `test("triggers live probe and renders live network reachability breakdown")`: Bấm nút "Kiểm tra kết nối live", xác nhận xuất hiện badge `live-probe-active-badge`, bảng phân tích `live-probe-breakdown`, thẻ kiểm tra `readiness-check-live_providers` cùng các huy hiệu phân định "CẦN XÁC MINH BẢO MẬT" và "CẦN ĐĂNG NHẬP PHIÊN".

---

## 4. Hướng Dẫn Vận Hành Trạm Làm Việc (Operational Workstation Guide)

Hệ thống đã đạt chứng nhận vận hành tại trạm làm việc (`APPROVED (WORKSTATION READY)`). Để phục vụ tìm kiếm video trực tiếp:

1. **Đối với Douyin**:
   - Khi tìm kiếm lần đầu hoặc khi tần suất tìm kiếm cao, Douyin có thể xuất hiện cửa sổ trượt CAPTCHA (`verification_required`).
   - Mở giao diện cấu hình cookie hoặc mở trình duyệt headful để giải thanh trượt. Phiên làm việc sẽ tự động được lưu trữ bền vững vào `data/browser/douyin/auth_state.json`.
2. **Đối với Xiaohongshu**:
   - Nền tảng Xiaohongshu yêu cầu phiên đăng nhập để duyệt feed tìm kiếm đầy đủ.
   - Sử dụng tính năng quét mã QR trên giao diện hoặc đăng nhập 1 lần trên trình duyệt Chromium trạm làm việc. Phiên và cookie sẽ được tự động lưu bền vững vào `data/browser/xiaohongshu/auth_state.json`.
