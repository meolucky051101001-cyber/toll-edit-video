# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **100% GREEN**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.36s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.09s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [0.33s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [8.73s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [2.16s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [14.84s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **414/414 PASS** | Toàn bộ 414 ca kiểm thử hồi quy đạt chuẩn (0 failed) [146.51s] |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **PASS** | 60 truy vấn chuẩn + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) [2.22s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **22/22 PASS** | 22 ca kiểm thử E2E trên 7 tập tin spec (bao gồm `health.spec.ts` mới) [31.12s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200, XHS: HTTP 200). Retrieval: minh bạch trạng thái CAPTCHA/Login [57.62s] |

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

### A. Khắc Phục Mismatch Hợp Đồng Giao Diện Dashboard `/health` & Kiểm Thử E2E
- **Vấn đề:** Backend endpoint `/api/system/release-readiness` trả về trường `checks` dưới dạng từ điển/object (`Record<string, CheckItem>`), trong khi giao diện cũ gọi `.map()` trực tiếp trên object này khiến React ném lỗi không bắt được tại runtime (`page.tsx:206`). Đồng thời, các trạng thái backend sử dụng chữ hoa (`READY`, `DEGRADED`, `BLOCKED`, `NOT_READY`).
- **Giải pháp:**
  1. Cập nhật `frontend/types/index.ts`: định nghĩa kiểu `ReleaseReadinessCheckItem { status, detail }`, `checks: Record<string, ReleaseReadinessCheckItem>` và union trạng thái `"READY" | "DEGRADED" | "BLOCKED" | "NOT_READY"`.
  2. Cập nhật `frontend/app/health/page.tsx`: chuyển đổi sang duyệt bằng `Object.entries(readiness.data.checks).map(...)` với badge hiển thị chuẩn xác (`PASS`, `WARN`/`STANDBY`, `FAIL`/`UNINITIALIZED`) và gắn thuộc tính kiểm thử `data-testid="readiness-check-${name}"`.
  3. Bổ sung bộ kiểm thử Playwright E2E mới [`frontend/tests/health.spec.ts`](file:///C:/Users/admin/Projects/ai-video-research-tool/frontend/tests/health.spec.ts): kiểm tra tiêu đề, thẻ tổng quan, hàng đợi tác vụ, độ trễ giám sát, ma trận năng lực và hành động nút "Làm mới", kiểm chứng không có ngoại lệ JavaScript trên console.

### B. Fail-Closed & Dò Mạng Song Song Bất Đồng Bộ (`backend/api/observability.py`)
- **Vấn đề:** Việc gọi dò mạng tuần tự tốn thời gian và nếu xảy ra ngoại lệ không được bao bọc có thể khiến backend trả về lỗi 500 hoặc báo sai trạng thái sẵn sàng.
- **Giải pháp:**
  1. Sử dụng `asyncio.gather(douyin_browser.probe_live(), browser.probe_live(), return_exceptions=True)` để thực hiện kiểm tra đồng thời cả 2 nền tảng.
  2. Áp dụng nguyên tắc **Fail-Closed**: nếu xảy ra bất kỳ lỗi ngoại lệ hoặc timeout nào, hệ thống lập tức đặt `checks["live_providers"] = {"status": "FAIL"}`, `is_ready = False` và `overall_status = "BLOCKED"`.
  3. Bổ sung bài test đơn vị `test_release_readiness_probe_fail_closed_on_error` trong `tests/test_observability_and_release_gate.py` (nâng tổng số test pytest lên 414 test pass).

### C. Bằng Chứng Mã Trạng Thái HTTP Trực Tiếp Cho Dò Mạng (`browser_service.py` & `douyin_browser_service.py`)
- **Vấn đề:** Cần bằng chứng kỹ thuật không thể chối cãi chứng minh trình duyệt đã kết nối thành công và không bị sập HTTP 500/503.
- **Giải pháp:**
  1. Lưu trữ thuộc tính `self.last_http_status: int | None` trên mỗi service trình duyệt.
  2. Bắt giữ `response.status` ngay trong hàm `page.goto()`.
  3. Trong `probe_live()`, kiểm tra nghiêm ngặt `200 <= status < 400` và định dạng chuỗi chi tiết: `HTTP 200: <trạng thái>`.
  4. Cập nhật `scripts/release_gate.py` để trích xuất trực tiếp thông tin này vào trường `probe_reachability` của `release_gate_report.json`:
     - Douyin: `HTTP 200: verification_required` (nhận diện qua iframe RMC NoCaptcha).
     - Xiaohongshu: `HTTP 200: login_required` (nhận diện qua dialog đăng nhập).

### D. Tự Động Hóa Đếm Spec & Đồng Bộ Báo Cáo Trong Release Gate (`scripts/release_gate.py`)
- Stage 10 tự động phát hiện số lượng file spec kiểm thử (`glob("*.spec.ts")`), hiển thị chính xác `22 tests passed across 7 specs`.
- Báo cáo [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json) ghi nhận đầy đủ chi tiết của 11 giai đoạn, được cam kết vào Git và đảm bảo không rò rỉ bất kỳ thông tin nhạy cảm nào.

---

## 4. Hướng Dẫn Vận Hành Trạm Làm Việc (Operational Workstation Guide)

Hệ thống đã đạt chứng nhận vận hành tại trạm làm việc (`APPROVED (WORKSTATION READY)`). Để phục vụ tìm kiếm video trực tiếp:

1. **Đối với Douyin**:
   - Khi tìm kiếm lần đầu hoặc khi tần suất tìm kiếm cao, Douyin có thể xuất hiện cửa sổ trượt CAPTCHA (`verification_required`).
   - Mở giao diện cấu hình cookie hoặc mở trình duyệt headful để giải thanh trượt. Phiên làm việc sẽ tự động được lưu trữ bền vững vào `data/browser/douyin/auth_state.json`.
2. **Đối với Xiaohongshu**:
   - Nền tảng Xiaohongshu yêu cầu phiên đăng nhập để duyệt feed tìm kiếm đầy đủ.
   - Sử dụng tính năng quét mã QR trên giao diện hoặc đăng nhập 1 lần trên trình duyệt Chromium trạm làm việc. Phiên và cookie sẽ được tự động lưu bền vững vào `data/browser/xiaohongshu/auth_state.json`.
