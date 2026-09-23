# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quá trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py`. Toàn bộ 10 giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **100% GREEN**. Giai đoạn 11 (Live Crawl & Reachability) đã được tái thiết kế triệt để nhằm phản ánh trung thực khả năng kết nối mạng thực tế và trạng thái xác thực người dùng mà không che giấu lỗi.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js |
| **8** | **Backend Pytest Suite** | `pytest -q` | **413/413 PASS** | Toàn bộ 413 ca kiểm thử hồi quy đạt chuẩn (0 failed) |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **PASS** | 60 truy vấn chuẩn + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **21/21 PASS** | 21 ca kiểm thử E2E trên 6 tập tin spec (giao diện tối giản mới) |
| **11** | **Live Crawl Acceptance** | `pytest tests/test_real_crawl_acceptance.py` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin & XHS). Retrieval: minh bạch trạng thái CAPTCHA/Login |

**Phán quyết Cổng Phát hành (Verdict)**:  
`APPROVED (WORKSTATION READY) - LIVE RETRIEVAL BLOCKED BY AUTH`  
*Chi tiết kiểm chứng lưu tại tệp versioned:* [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json).

---

## 2. Các Cải Tiến Kỹ Thuật Đã Thực Hiện

### A. Tái Thiết Kế Kiểm Thử Live Crawl & Bỏ Hoàn Toàn Việc Nuốt Lỗi (`tests/test_real_crawl_acceptance.py`)
- **Vấn đề trước đây:** Khi Douyin yêu cầu giải CAPTCHA hoặc Xiaohongshu yêu cầu đăng nhập, bài test cũ bắt ngoại lệ `UserActionRequired` và chỉ kiểm tra thuộc tính lỗi rồi kết luận PASS, dẫn đến báo cáo "Live Crawl PASS" sai lệch dù thực tế thu thập 0 video.
- **Giải pháp triệt để:**
  1. **Bóc tách bài test**:
     - `test_offline_crawl_provider_contract_and_filters`: Luôn chạy kiểm thử hợp đồng dữ liệu và ranking trên bộ mẫu chuẩn đã kiểm chứng.
     - `test_live_network_probe_reachability`: Khởi động Chromium thật, gọi `probe_live()` kiểm tra khả năng mở trang của cả Douyin và Xiaohongshu (bảo đảm HTTP 200, không bị lỗi 503, không sập Playwright).
     - `test_live_crawl_douyin_real_results`: Tìm kiếm từ khóa thật trên Douyin, bắt buộc `len(results) > 0`. Nếu gặp thử thách bảo mật hoặc 0 kết quả, bài test ghi nhận đúng trạng thái `BLOCKED` / `pytest.fail()`.
     - `test_live_crawl_xiaohongshu_real_results`: Tìm kiếm từ khóa thật trên Xiaohongshu, bắt buộc `len(results) > 0`. Nếu gặp yêu cầu đăng nhập phiên, ghi nhận chính xác trạng thái `BLOCKED (LOGIN_REQUIRED)`.

### B. Bóc Tách Readiness Probe Cơ Bản & Live Provider (`backend/api/observability.py`)
- **Vấn đề trước đây:** Endpoint `/api/system/release-readiness` khi gọi mà không có tham số `probe=true` vẫn trả về `status: READY` ngay cả khi provider chưa được khởi tạo.
- **Giải pháp triệt để:**
  1. Khi `probe=False`: Nếu `browser` hoặc `douyin_browser` chưa khởi tạo (`None`), hệ thống gán `status: "UNINITIALIZED"`, thêm vào cảnh báo và trả về `status: "NOT_READY"`.
  2. Nếu browser ở trạng thái standby (`closed`), trả về `status: "STANDBY"` và tổng thể `"DEGRADED"`, yêu cầu gọi `?probe=true` để kiểm định mạng.
  3. Khi `probe=True`: Bổ sung kiểm tra `live_providers`, thực thi song song `probe_live()` cho cả 2 nền tảng và chỉ xác nhận `PASS` khi mạng của cả hai đều thông suốt.

### C. Nâng Cấp Bộ Dò Bắt Iframe CAPTCHA Trên Trình Duyệt (`douyin_browser_service.py` & `browser_service.py`)
- Phát hiện lỗi Douyin nhúng iframe xác thực bảo mật `RMC NoCaptcha` (`rc-verifycenter/rmc-nocaptcha/...`) khiến việc đọc DOM chính không thấy text lỗi mà chỉ thấy 0 link video dẫn đến lỗi nhầm `SelectorChangedError`.
- Bổ sung cơ chế duyệt qua `page.frames` để phát hiện ngay lập tức các iframe xác thực bảo mật (`verifycenter`, `nocaptcha`, `captcha`) và chuyển trạng thái trình duyệt sang `verification_required`, kích hoạt thông báo người dùng kịp thời.

### D. Nâng Cấp Cổng Phát Hành Release Gate (`scripts/release_gate.py`)
- Stage 11 được chia thành 3 bước kiểm định rõ ràng:
  - **11.A**: Platform Reachability Probe (xác thực mạng và Chromium cho cả 2 bên).
  - **11.B**: Douyin Live Retrieval (kiểm tra thu thập video thực tế).
  - **11.C**: Xiaohongshu Live Retrieval (kiểm tra thu thập video/ghi chú thực tế).
- Báo cáo `release_gate_report.json` được đưa vào Git để có thể kiểm chứng (loại bỏ khỏi `.gitignore`), cam kết tuyệt đối không lưu trữ cookie, token phiên hay secret trong báo cáo.

---

## 3. Hướng Dẫn Vận Hành Trạm Làm Việc (Operational Workstation Guide)

Hệ thống đã đạt điều kiện vận hành tại trạm làm việc (Workstation Ready). Để khai thác tối đa việc tìm kiếm video trực tiếp:

1. **Đối với Douyin**:
   - Khi tìm kiếm lần đầu hoặc khi tần suất tìm kiếm cao, Douyin có thể xuất hiện cửa sổ trượt CAPTCHA (`verification_required`).
   - Mở giao diện cấu hình cookie hoặc chạy chế độ headful một lần để kéo thanh trượt xác minh. Phiên làm việc sẽ tự động được lưu trữ vào `data/browser/douyin/auth_state.json`.
2. **Đối với Xiaohongshu**:
   - Nền tảng Xiaohongshu yêu cầu phiên đăng nhập để duyệt feed tìm kiếm đầy đủ.
   - Sử dụng tính năng quét mã QR trên giao diện hoặc đăng nhập 1 lần trên trình duyệt Chromium trạm làm việc. Phiên và cookie sẽ được tự động lưu bền vững vào `data/browser/xiaohongshu/auth_state.json`.
