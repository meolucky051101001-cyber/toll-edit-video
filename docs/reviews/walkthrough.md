# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py --live`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **100% GREEN**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.34s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.08s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [0.33s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [5.75s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [2.13s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [15.26s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **421/421 PASS** | Toàn bộ 421 ca kiểm thử hồi quy đạt chuẩn (0 failed) [138.94s] |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **PASS** | 60 truy vấn chuẩn + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) [2.77s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **25/25 PASS** | 25 ca kiểm thử E2E trên 7 tập tin spec (kiểm chứng màu DEGRADED, badge CAPTCHA XHS, trạng thái authenticated) [38.07s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200: page_available, XHS: HTTP 200: page_available). Retrieval: minh bạch trạng thái CAPTCHA/Login [63.67s] |

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

### A. Xác Thực Phiên Thật Xiaohongshu & Cấu Trúc Dữ Liệu Live Probe
- **Vấn đề trước đây:** `inspect()` chỉ kiểm tra sự tồn tại của session cookie và trả về `page_available` cho cả phiên đã đăng nhập lẫn chưa đăng nhập. Probe không truyền trạng thái xác thực lên API khiến API quy tất cả về "chưa xác minh phiên" (DEGRADED). Cookie cũ bị coi nhầm là authenticated dù modal đăng nhập đang chặn.
- **Giải pháp triệt để:**
  1. Trong `backend/services/browser_service.py` và `backend/services/douyin_browser_service.py`, áp dụng điều kiện xác thực nghiêm ngặt 3 yếu tố:
     `is_authenticated = bool(has_session_cookie and has_user_element and not login_modal_visible)`
  2. `self.state` chuyển sang `"authenticated"` khi xác thực thành công; giữ `"page_available"` khi là khách vãng lai; `"verification_required"` khi gặp CAPTCHA; `"login_required"` khi có modal hoặc trang đăng nhập.
  3. Bổ sung `_parse_probe_state` trong `backend/api/observability.py` để tách bạch rõ ràng cấu trúc: `reachable: bool`, `http_status: int | None`, `session_state: str`, `state: str`.
  4. Tạo mới bộ test `tests/test_browser_service.py` gồm 5 kịch bản kiểm thử đơn vị:
     - `test_xhs_inspect_and_probe_authenticated`
     - `test_xhs_inspect_and_probe_unauthenticated_guest`
     - `test_xhs_inspect_and_probe_captcha_challenge`
     - `test_xhs_inspect_and_probe_stale_cookie_with_login_modal`
     - `test_xhs_probe_network_error_503_or_timeout`

### B. Bổ Sung Nhãn & Hướng Dẫn CAPTCHA Xiaohongshu Trên Dashboard (`frontend/app/health/page.tsx`)
- **Vấn đề trước đây:** Nhánh xử lý XHS thiếu trường hợp `verification_required`, dẫn đến khi gặp thử thách bảo mật thì rơi vào trạng thái "CHƯA XÁC ĐỊNH PHIÊN".
- **Giải pháp triệt để:**
  1. Thêm badge `CẦN XÁC MINH BẢO MẬT (CAPTCHA)` với màu cảnh báo hổ phách (`#fef3c7`, `#92400e`).
  2. Hiển thị thông báo chi tiết: *"Phát hiện thử thách xác minh bảo mật (CAPTCHA - cần giải trên trạm làm việc)"*.
  3. Cung cấp chỉ dẫn vận hành: `* Cần mở trình duyệt hoặc giải CAPTCHA trạm làm việc để duyệt tìm kiếm.`.
  4. Hỗ trợ badge `ĐÃ ĐĂNG NHẬP (SẴN SÀNG)` màu xanh lá (`#dcfce7`, `#166534`) cho cả Xiaohongshu và Douyin khi phiên đã đăng nhập thành công.

### C. Khắc Phục Lỗi Hiển Thị Màu Thẻ Release Readiness (DEGRADED Luôn Là Màu Hổ Phách)
- **Vấn đề trước đây:** Trong `frontend/app/health/page.tsx`, logic chọn màu của thẻ tổng quan Release Readiness ưu tiên `activeReadiness?.is_ready ? "#16a34a" : ...`, khiến thẻ bị tô màu xanh lá ngay cả khi chữ hiển thị là `DEGRADED`.
- **Giải pháp triệt để:**
  1. Đổi logic màu dựa trực tiếp vào chuỗi trạng thái:
     - `READY` -> Xanh lá (`#16a34a`)
     - `DEGRADED` -> Hổ phách/Vàng (`#ca8a04`)
     - `BLOCKED` / `NOT_READY` -> Đỏ (`#dc2626`)
  2. Cập nhật Playwright E2E test `frontend/tests/health.spec.ts` kiểm chứng chính xác: khi ở trạng thái `DEGRADED`, thẻ Release Readiness có màu `rgb(202, 138, 4)` và **tuyệt đối không phải** `rgb(22, 163, 74)`.

---

## 4. Hướng Dẫn Vận Hành Trạm Làm Việc (Operational Workstation Guide)

Hệ thống đã đạt chứng nhận vận hành tại trạm làm việc (`APPROVED (WORKSTATION READY)`). Để phục vụ tìm kiếm video trực tiếp:

1. **Đối với Douyin**:
   - Khi tìm kiếm lần đầu hoặc khi tần suất tìm kiếm cao, Douyin có thể xuất hiện cửa sổ trượt CAPTCHA (`verification_required`).
   - Mở giao diện cấu hình cookie hoặc mở trình duyệt headful để giải thanh trượt. Phiên làm việc sẽ tự động được lưu trữ bền vững vào `data/browser/douyin/auth_state.json`.
2. **Đối với Xiaohongshu**:
   - Nền tảng Xiaohongshu yêu cầu phiên đăng nhập để duyệt feed tìm kiếm đầy đủ.
   - Sử dụng tính năng quét mã QR trên giao diện hoặc đăng nhập 1 lần trên trình duyệt Chromium trạm làm việc. Phiên và cookie sẽ được tự động lưu bền vững vào `data/browser/xiaohongshu/auth_state.json`.
