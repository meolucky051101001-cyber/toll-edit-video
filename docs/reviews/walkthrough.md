# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py --live`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp và E2E đều đạt chuẩn **100% GREEN**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :---: | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [1.29s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.23s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [1.93s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [35.79s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [3.73s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [19.95s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **416/416 PASS** | Toàn bộ 416 ca kiểm thử hồi quy đạt chuẩn (0 failed) [156.52s] |
| **9** | **Fixture Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **PASS** | 60 truy vấn chuẩn + 4 bộ dữ liệu crawl thực tế (20 mẫu nguồn) [2.55s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **23/23 PASS** | 23 ca kiểm thử E2E trên 7 tập tin spec (bao gồm `health.spec.ts` kiểm chứng dữ liệu thật) [36.05s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200: page_available, XHS: HTTP 200: page_available). Retrieval: minh bạch trạng thái CAPTCHA/Login [93.14s] |

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

### A. Phân Định Rạch Ròi Trạng Thái Phiên Xiaohongshu Trên Dashboard (`frontend/app/health/page.tsx`)
- **Vấn đề trước đây:** Khi probe Xiaohongshu trả về `HTTP 200: page_available`, dashboard gán nhãn `ĐÃ KẾT NỐI (READY)`. Tuy nhiên, trang công khai `/explore` tuy tải được HTTP 200 nhưng khi thực hiện tìm kiếm thật lại bị chặn đăng nhập (`BLOCKED (LOGIN_REQUIRED)`), dẫn đến báo cáo trạng thái READY giả.
- **Giải pháp triệt để:**
  1. Tách bạch hoàn toàn giữa **kết nối mạng** (`HTTP 200: Đã kết nối`) và **trạng thái phiên làm việc**.
  2. Phân loại chính xác:
     - `page_available`: Gán nhãn `CHƯA XÁC MINH PHIÊN (CẦN ĐĂNG NHẬP ĐỂ TÌM KIẾM)` với màu cảnh báo hổ phách (`#fef3c7`), ghi rõ *"Chưa xác minh phiên đăng nhập (Tìm kiếm thật có thể bị chặn)"*.
     - `login_required`: Gán nhãn `CẦN ĐĂNG NHẬP PHIÊN`.
     - `verification_required`: Gán nhãn `CẦN XÁC MINH BẢO MẬT (CAPTCHA)`.
     - Chỉ khi `authenticated` hoặc `ready` mới gán nhãn `ĐÃ ĐĂNG NHẬP (SẴN SÀNG)`.
  3. Cập nhật bài test E2E Playwright `frontend/tests/health.spec.ts` kiểm chứng thẻ Xiaohongshu hiển thị đúng badge `CHƯA XÁC MINH PHIÊN` và **tuyệt đối không chứa** `ĐÃ KẾT NỐI (READY)`.

### B. Loại Bỏ Trạng Thái STANDBY Ảo Sau Khi Live Probe Thành Công (`backend/api/observability.py`)
- **Vấn đề trước đây:** Khi gọi `/api/system/release-readiness?probe=true` và các provider probe thành công (`HTTP 200: ready`), `checks["providers"]` vẫn bị kẹt ở trạng thái `STANDBY` và `warnings` vẫn chứa `"Provider browser sessions on standby (not probed)"`. Điều này làm trạng thái tổng thể bị suy giảm thành `DEGRADED` thay vì `READY`.
- **Giải pháp triệt để:**
  1. Khi live probe thành công, cập nhật `checks["providers"]["status"] = "PASS"` với chi tiết `"Provider browser services verified live"`.
  2. Lọc bỏ hoàn toàn các cảnh báo standby (`"standby (not probed)"`) khỏi danh sách `warnings`.
  3. Nếu không có thử thách bảo mật hoặc phiên chưa xác minh, endpoint trả về `status: "READY"` và `is_ready: True`.
  4. Nếu có thử thách bảo mật (`verification_required`, `login_required`, hoặc `page_available` trên XHS), ghi nhận cảnh báo chính xác và hạ cấp có kiểm soát về `status: "DEGRADED"`.
  5. Bổ sung Case 3 và Case 4 trong `test_release_readiness_probe_fail_closed_on_error` để kiểm chứng cả hai hành vi.

### C. Đặt Lại Dữ Liệu Probe Cũ Khi Làm Mới Dashboard (`frontend/app/health/page.tsx`)
- **Vấn đề trước đây:** Biến `activeReadiness` ưu tiên hiển thị `liveProbeData` nếu có. Khi người dùng bấm nút "Làm mới", bắt đầu một đợt probe mới, hoặc khi probe gặp lỗi, `liveProbeData` không được reset, dẫn đến giao diện tiếp tục hiển thị kết quả probe cũ.
- **Giải pháp triệt để:**
  1. Trong `handleRunLiveProbe()`, gọi `setLiveProbeData(null)` ngay khi bắt đầu và trong khối `catch`.
  2. Trong `refreshAll()`, gọi `setLiveProbeData(null)` để xóa bỏ toàn bộ kết quả probe trước đó, đưa dashboard về trạng thái dữ liệu nền tươi mới.
  3. Bổ sung bài kiểm thử E2E Playwright trong `health.spec.ts` bấm nút "Làm mới" và xác nhận các phần tử `live-probe-active-badge` và `live-probe-breakdown` biến mất hoàn toàn khỏi DOM.

### D. Tinh Chỉnh Chuyển Tiếp Giữa E2E Và Live Probe Trong Release Gate (`scripts/release_gate.py`)
- **Vấn đề trước đây:** Ngay sau khi Stage 10 (Playwright E2E) kết thúc, Stage 11 (Live Probe) được kích hoạt ngay lập tức. Các tiến trình trình duyệt và cổng mạng chưa kịp giải phóng hoàn toàn trên Windows có thể gây xung đột socket/profile. Đồng thời, regex trích xuất trạng thái probe bắt nhầm dòng code `print` trong traceback khi test thất bại.
- **Giải pháp triệt để:**
  1. Bổ sung khoảng trễ `time.sleep(2.0)` giữa Stage 10 và Stage 11 để bảo đảm 100% tài nguyên socket/Chromium được thu hồi sạch sẽ.
  2. Nâng cấp regex sang định dạng multiline neo đầu dòng: `re.search(r"^\s*PROBE_DOUYIN:\s*(HTTP\s+\d+.*)", p_probe.stdout, re.MULTILINE)` để chỉ khớp với dòng log stdout thực tế.

---

## 4. Hướng Dẫn Vận Hành Trạm Làm Việc (Operational Workstation Guide)

Hệ thống đã đạt chứng nhận vận hành tại trạm làm việc (`APPROVED (WORKSTATION READY)`). Để phục vụ tìm kiếm video trực tiếp:

1. **Đối với Douyin**:
   - Khi tìm kiếm lần đầu hoặc khi tần suất tìm kiếm cao, Douyin có thể xuất hiện cửa sổ trượt CAPTCHA (`verification_required`).
   - Mở giao diện cấu hình cookie hoặc mở trình duyệt headful để giải thanh trượt. Phiên làm việc sẽ tự động được lưu trữ bền vững vào `data/browser/douyin/auth_state.json`.
2. **Đối với Xiaohongshu**:
   - Nền tảng Xiaohongshu yêu cầu phiên đăng nhập để duyệt feed tìm kiếm đầy đủ.
   - Sử dụng tính năng quét mã QR trên giao diện hoặc đăng nhập 1 lần trên trình duyệt Chromium trạm làm việc. Phiên và cookie sẽ được tự động lưu bền vững vào `data/browser/xiaohongshu/auth_state.json`.
