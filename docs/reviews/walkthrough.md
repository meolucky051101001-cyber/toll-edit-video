# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py --live`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **100% GREEN**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.51s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.15s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [0.72s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [9.8s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [2.89s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [18.29s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **426/426 PASS** | Toàn bộ 426 ca kiểm thử hồi quy đạt chuẩn (0 failed) [142.48s] |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **PASS** | 60 truy vấn chuẩn + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) [3.06s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **25/25 PASS** | 25 ca kiểm thử E2E trên 7 tập tin spec [41.3s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200: verification_required, XHS: HTTP 200: page_available). Retrieval: minh bạch trạng thái CAPTCHA/Login [140.88s] |

**Phán quyết Cổng Phát hành (Verdict)**:  
`APPROVED (WORKSTATION READY) - LIVE RETRIEVAL BLOCKED BY AUTH` (Tổng thời gian: 365.21s, mã thoát: 0)  
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

### A. Loại Bỏ Hoàn Toàn Nguy Cơ Nhận Diện Nhầm Khách Xiaohongshu Là `authenticated`
- **Khám phá thực tế trên Chromium**: Qua khảo sát DOM thực tế, Xiaohongshu tự động cấp cookie `web_session` ngay cho khách vãng lai và sidebar của trang khách vẫn luôn hiển thị các tab điều hướng như "我" hay "Me". Do đó, nếu dựa vào các selector chung chung này kết hợp với cookie, hệ thống sẽ kết luận nhầm trang khách là đã xác thực.
- **Giải pháp triệt để**:
  1. Loại bỏ toàn bộ các bộ chọn văn bản chung chung: xóa `.side-bar :text('我')`, `.side-bar :text('Me')`, `.side-bar :text('Profile')`, `.side-bar :text('My Profile')`.
  2. Bổ sung kiểm tra nút đăng nhập: nếu phát hiện bất kỳ nút/liên kết đăng nhập nào hiển thị (`button:has-text('登录'):visible`, `.login-btn:visible`, `[class*='login-btn']:visible`, `.side-bar :text('登录'):visible`...), hệ thống lập tức phủ quyết `is_authenticated = False`. Thêm `:visible` vào tất cả các selector nút đăng nhập trên cả Xiaohongshu và Douyin, đồng thời re-evaluate sau khi thử đóng modal đăng nhập.
  3. Thu hẹp selector định danh người dùng: chỉ chấp nhận link profile trong thanh điều hướng sidebar và user nav (`.side-bar a[href*='/user/profile/']:visible, .user-nav a[href*='/user/profile/']:visible`), loại bỏ hoàn toàn `a[href*='/user/profile/']` toàn cục vốn khớp với link tác giả bài viết trong feed.
  4. Sửa lỗi kiểm tra `window.__INITIAL_STATE__.user.loggedIn`: kiểm tra an toàn theo kiểu dữ liệu (`typeof raw === 'boolean'` trước tiên để trả về `false`/`true` trực tiếp, chỉ dùng `in` khi `typeof raw === 'object'`), loại bỏ triệt để lỗi `TypeError: Cannot use 'in' operator to search for '_value' in false`.
  5. Áp dụng cơ chế kiểm tra nút đăng nhập tương tự cho Douyin (`[data-e2e='login-button']:visible`, `button:has-text('登录'):visible`...).
  6. Bổ sung 3 bài test đơn vị chuyên biệt trong `tests/test_browser_service.py`:
     - `test_xhs_js_snippet_logged_in_evaluation`: Kiểm chứng trực tiếp trên Node.js runtime cho cả boolean nguyên thủy, Vue 3 reactive ref (`_value`), và Vue ref (`value`).
     - `test_xhs_guest_page_with_note_author_links_not_authenticated`: Trang khách chứa link profile tác giả bài viết không bị nhận diện nhầm.
     - `test_xhs_authenticated_with_hidden_login_button`: Nút đăng nhập ẩn trong DOM không làm phủ quyết phiên hợp lệ.

### B. Làm Rõ Hợp Đồng API: Tách Biệt `workstation_ready` và `retrieval_ready`
- **Vấn đề trước đây**: Khi kết nối mạng đạt HTTP 200 nhưng bị chặn bởi CAPTCHA hoặc yêu cầu đăng nhập, API trả về `status: "DEGRADED"` nhưng `is_ready` vẫn là `true`. Ngoài ra `workstation_ready` trước đây được tính từ `overall_status not in ("BLOCKED", "NOT_READY")`, dẫn tới khi chưa probe (`probe=False`, provider ở `STANDBY`) vẫn trả về `True`.
- **Giải pháp triệt để**:
  1. Làm rõ hợp đồng:
     - `retrieval_ready: bool`: Chỉ `True` khi `status == "READY"`.
     - `workstation_ready: bool`: Tính từ các check cụ thể `db_ok and worker_ok and live_probe_ok`. Khi `probe=False`, `live_probe_ok = False` $\to$ `workstation_ready = False` (chưa xác minh). Khi `probe=True`, `workstation_ready = True` khi database, worker pool và live probe đều đạt.
     - `is_ready: bool`: Đồng nhất với `retrieval_ready` (`False` khi `DEGRADED`, `BLOCKED`, `NOT_READY`). Client đọc `is_ready` sẽ không bao giờ bị hiểu nhầm khi tìm kiếm video bị gián đoạn.
  2. Cập nhật kiểu `ReleaseReadinessResponse` trong `frontend/types/index.ts`.
  3. Cập nhật toàn bộ bài test kiểm chứng trong `tests/test_observability_and_release_gate.py` và `frontend/tests/health.spec.ts`.

### C. Nâng Cấp Độ Ổn Định Probe & Cooldown Giữa Các Giai Đoạn
- Nâng timeout chuyển trang `probe_live()` từ 20s lên 30s cho cả Douyin và Xiaohongshu để tránh tình trạng timeout giả do độ trễ mạng Internet đột biến.
- Tăng thời gian cooldown giữa Stage 10 (Playwright E2E) và Stage 11 (Live Probe) trong `scripts/release_gate.py` lên 5.0s và thêm 1 lần retry tự động cho bước probe reachability nhằm đảm bảo thu hồi sạch tài nguyên socket/Chromium trên Windows.

---

## 4. Hướng Dẫn Vận Hành Trạm Làm Việc (Operational Workstation Guide)

Hệ thống đã đạt chứng nhận vận hành tại trạm làm việc (`APPROVED (WORKSTATION READY)`). Để phục vụ tìm kiếm video trực tiếp:

1. **Đối với Douyin**:
   - Khi tìm kiếm lần đầu hoặc khi tần suất tìm kiếm cao, Douyin có thể xuất hiện cửa sổ trượt CAPTCHA (`verification_required`).
   - Mở giao diện cấu hình cookie hoặc mở trình duyệt headful để giải thanh trượt. Phiên làm việc sẽ tự động được lưu trữ bền vững vào `data/browser/douyin/auth_state.json`.
2. **Đối với Xiaohongshu**:
   - Nền tảng Xiaohongshu yêu cầu phiên đăng nhập để duyệt feed tìm kiếm đầy đủ.
   - Sử dụng tính năng quét mã QR trên giao diện hoặc đăng nhập 1 lần trên trình duyệt Chromium trạm làm việc. Phiên và cookie sẽ được tự động lưu bền vững vào `data/browser/xiaohongshu/auth_state.json`.
3. **Tiêu chuẩn FULL GREEN**:
   - Chạy lệnh `python scripts/release_gate.py --live --require-live` sau khi đã hoàn tất giải CAPTCHA / đăng nhập trên trạm làm việc để nghiệm thu kết quả lấy video trực tiếp.
