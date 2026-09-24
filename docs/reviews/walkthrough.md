# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py --live --require-live` tại commit mới nhất. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp điều phối SQLite, benchmark dữ liệu crawl có xác minh nguồn gốc và E2E đều đạt chuẩn **PASS (0 lỗi)**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :--- | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.57s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.17s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [1.03s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [13.65s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [4.23s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [16.29s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **441/441 PASS** | 441 passed, 0 failed, 3 skipped [169.31s] |
| **9** | **Fixture & Crawled Benchmark** | `pytest tests/test_evaluation_benchmark.py tests/test_query_precision_evaluation.py ...` | **PASS** | 60 truy vấn domain + 4 truy vấn crawl thực tế (20 mẫu: 16 video, 4 img/txt, provenance verified) [2.33s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **26/26 PASS** | 26 ca kiểm thử E2E trên 7 tập tin spec (bao gồm ca kiểm thử điều phối `active_platform` đa nền tảng) [34.63s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live --require-live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200: verification_required, XHS: HTTP 200: page_available). Retrieval: Douyin BLOCKED (CHALLENGE_REQUIRED), XHS BLOCKED (LOGIN_REQUIRED) [69.15s] |

**Phán quyết Cổng Phát hành (Verdict)**:  
`APPROVED (WORKSTATION READY) - LIVE RETRIEVAL BLOCKED BY AUTH` (Tổng thời gian: 316.47s)  
*Chi tiết kiểm chứng đồng bộ tuyệt đối lưu tại tệp versioned:* [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json).

---

## 2. Tiêu Chuẩn Thực Thi Lệnh & Chế Độ Nghiêm Ngặt (Strict Gate)

Bằng chứng thực thi Strict Gate đã được ghi nhận trực tiếp vào [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json) với `executed: true` và `exit_code: 2`:

```json
"strict_live_run": {
  "executed": true,
  "command": "python scripts/release_gate.py --live --require-live",
  "exit_code": 2,
  "timestamp": "2026-09-24T07:12:30Z",
  "douyin": "BLOCKED (CHALLENGE_REQUIRED)",
  "xiaohongshu": "BLOCKED (LOGIN_REQUIRED)"
}
```

| Mục Đích Kiểm Thử | Lệnh Tiêu Chuẩn | Hành Vi & Phán Quyết | Mã Thoát (Exit Code) Thực Tế |
| :--- | :--- | :--- | :---: |
| **Kiểm thử nghiêm ngặt thu thập thật (Strict Gate)** | `python scripts/release_gate.py --live --require-live` | Bắt buộc cả Douyin và XHS phải trả về `>= 1 video thật` đúng ý định tìm kiếm. Khi phiên live chưa tự giải CAPTCHA / đăng nhập trên trạm làm việc, cổng phát hành từ chối và thoát mã lỗi 2 (fail-closed trung thực). | `2` (fail-closed, không báo FULL GREEN ảo) |
| **Nghiệm thu toàn bộ 11 Stage (Vận hành máy trạm)** | `python scripts/release_gate.py --live` | Chạy toàn bộ 11 stage. Ở Stage 11, kiểm tra Chromium thật mở được Douyin và XHS (HTTP 200). Khi gặp CAPTCHA/Login, hệ thống ghi nhận đúng trạng thái và kết luận `APPROVED (WORKSTATION READY)`. | `0` |
| **Kiểm thử hồi quy backend** | `pytest -q` | Chạy toàn bộ 441 bài kiểm thử tự động, 3 bài test live crawl được bỏ qua an toàn khi chưa bật `LIVE_CRAWL_ENABLED=1`. | `0` |
| **Chạy cổng phát hành offline (10 Stage)** | `python scripts/release_gate.py` | Chạy đầy đủ từ Stage 1 đến Stage 10 (bỏ qua Stage 11 Live Crawl). | `0` |

---

## 3. Phân Định Minh Bạch Các Tầng Kiểm Thử

### A. Tầng Điều Phối Pipeline (Pipeline Orchestration Integration)
- **Tập tin kiểm thử**: [`tests/test_search_service_resume.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_search_service_resume.py).
- **Phạm vi kiểm định**: Sử dụng mock provider có kiểm soát kết hợp với SQLite DB thật để kiểm thử toàn diện luồng điều phối của `SearchService.pipeline()`.
  - Hai thứ tự nền tảng (Douyin $\to$ Xiaohongshu và Xiaohongshu $\to$ Douyin): bảo toàn kết quả một phần khi nền tảng sau bị chặn, ghi nhận đúng `active_platform` và `waiting_reason`.
  - Cơ chế chống trùng lặp (`deduplication`): không tạo bản ghi `JobVideo` thừa khi tiếp tục tìm kiếm sau khi bị ngắt.
  - Xử lý timeout thao tác thủ công với thông điệp rõ ràng cho từng nền tảng.
  - Endpoint `POST /api/search/jobs/{job_id}/resume`: trả về đúng **HTTP 409 Conflict** khi chưa hoàn tất xác thực và **HTTP 200 OK** khi phiên đã sẵn sàng.

### B. Tầng Kiểm Thử Giao Diện & E2E (UI & Frontend Verification)
- **Playwright E2E**: 26/26 bài test pass trên 7 file spec (`frontend/tests/`).
- **Kiểm thử điều phối đa nền tảng mới** ([`frontend/tests/search.spec.ts`](file:///C:/Users/admin/Projects/ai-video-research-tool/frontend/tests/search.spec.ts)):
  - Ca kiểm thử `multi-platform search waiting on second platform targets active_platform in banner and opens correct browser` mô phỏng job có `platforms: ["douyin", "xiaohongshu"]` nhưng `active_platform: "xiaohongshu"`.
  - Xác nhận banner hiển thị đúng: `🔐 Nền tảng Xiaohongshu yêu cầu đăng nhập tài khoản`.
  - Xác nhận nút điều khiển là `Mở cửa sổ Xiaohongshu` và khi bấm gọi chính xác `POST /api/browser/xiaohongshu/open` (không gọi nhầm sang Douyin).
  - Xác nhận khi chuyển sang `active_platform: "douyin"` (`waiting_for_user`), banner hiển thị `⚠️ Nền tảng Douyin yêu cầu xác minh bảo mật`, nút bấm đổi thành `Mở cửa sổ Douyin` và gọi đúng `POST /api/browser/douyin/open`.

### C. Tầng Đo Lường Độ Chính Xác Độc Lập (Retrieval Precision Benchmark)
- **Tập tin kiểm thử**: [`tests/test_query_precision_evaluation.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_query_precision_evaluation.py).
- **Ngưỡng phân loại độc lập**: Định nghĩa rõ ràng `CLASSIFICATION_THRESHOLD = 15.0`.
- **Confusion Matrix thực chất**:
  - Nhãn dự đoán `is_pred = (final_score >= 15.0)` được so sánh độc lập với nhãn mặt đất (`ground_truth_relevance`) do người duyệt gán.
  - Phân bổ độc lập: $TP = (is\_gt \land is\_pred)$, $FP = (\neg is\_gt \land is\_pred)$, $TN = (\neg is\_gt \land \neg is\_pred)$, $FN = (is\_gt \land \neg is\_pred)$.
  - Tách bạch hoàn toàn kết quả video khỏi ghi chú ảnh/chữ (`image_text`):
    - **Douyin Toàn bộ (20 mẫu)**: Precision = 1.00, Accuracy = 1.00 ($TP=12, FP=0, TN=8, FN=0$).
    - **Douyin Phân hệ Video (16 video)**: Precision = 1.00, Accuracy = 1.00 ($TP=8, FP=0, TN=8, FN=0$).
    - **Douyin Phân hệ Ghi chú ảnh/chữ (4 bài)**: ($TP=4, FP=0, TN=0, FN=0$).
- **Báo cáo trung thực nền tảng Xiaohongshu**:
  - `Platform [XIAOHONGSHU]: PENDING DATA (0 crawled samples with verified provenance. Requires live authenticated crawl on workstation to populate)`.
  - Tuyệt đối không tạo số liệu giả lập hay báo 1.00 khi chưa có dữ liệu crawl thực tế trên Xiaohongshu.
- **Xác thực Nguồn gốc (Provenance Verification)**:
  - Cả bài test lẫn Stage 9 của Release Gate đều kiểm tra từng mẫu dữ liệu: ID, URL canonical đúng định dạng nền tảng, timestamp ISO 8601, `media_type`, tiêu đề, tác giả, nhãn boolean và lý do đánh giá (`rationale`). Không chỉ đếm số dòng JSON đơn thuần.

### D. Tầng Kiểm Thử Live Crawl & Đánh Giá Độ Khớp Ý Định (Stage 11 Live Retrieval)
- **Tập tin kiểm thử**: [`tests/test_real_crawl_acceptance.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_real_crawl_acceptance.py).
- **Nâng cấp truy vấn ưu tiên**: Đổi từ khóa chung `"decor"` / `"sticker"` sang truy vấn ghép phức hợp: **`"nhíp gắp sticker"`**.
- **Truy vấn tiếng Trung gửi nền tảng**: Sinh tự động thông qua bộ lập kế hoạch `plan_query("nhíp gắp sticker")`:
  - Từ khóa chính: **`贴纸镊子`**
  - Từ khóa phân tầng: `["贴纸镊子", "手账贴纸镊子", "咕卡镊子"]`
- **Bộ lọc phương tiện bắt buộc**: `filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="video")`.
- **Kiểm định độ liên quan phức hợp**:
  - Không chấp nhận bất kỳ video nào chỉ vì có `media_type == "video"`.
  - Bắt buộc kiểm tra độ khớp đồng thời cả 2 ý niệm cốt lõi: dụng cụ nhíp/gắp (`镊子`, `nhíp`, `nhip`, `gắp`, `gap`) VÀ đồ dán sticker (`贴纸`, `sticker`, `咕卡`, `手账`).
  - Loại bỏ hoàn toàn các video chỉ bán sticker thông thường mà không có nhíp.
- **Trạng thái thực tế trên máy trạm hiện tại**:
  - Douyin: HTTP 200 $\to$ `BLOCKED (CHALLENGE_REQUIRED)` (thanh trượt CAPTCHA).
  - Xiaohongshu: HTTP 200 $\to$ `BLOCKED (LOGIN_REQUIRED)` (yêu cầu đăng nhập tài khoản).
  - Khi bị chặn, test chủ động gọi `pytest.fail()`, giữ nguyên tắc fail-closed và không báo xanh ảo.

---

## 4. Hướng Dẫn Vận Hành & Nghiệm Thu Live Thực Tế

Để chuyển trạng thái hệ thống từ `APPROVED (WORKSTATION READY)` sang `APPROVED (FULL GREEN)`:

1. **Bước 1: Tự hoàn thành xác minh trên Chromium của tool**:
   - Mở giao diện công cụ tại `http://localhost:3000/settings`.
   - Bấm **Mở cửa sổ Douyin** $\to$ giải thanh trượt CAPTCHA trực tiếp trên cửa sổ trình duyệt Chromium.
   - Bấm **Mở cửa sổ Xiaohongshu** $\to$ quét mã QR đăng nhập tài khoản.
   - Profile Chromium cục bộ tại `data/browser/douyin` và `data/browser/xiaohongshu` sẽ tự động duy trì phiên.
2. **Bước 2: Chạy Cổng Phát Hành Nghiêm Ngặt**:
   - Chạy lệnh: `python scripts/release_gate.py --live --require-live`
   - Hệ thống sẽ tìm kiếm từ khóa `"nhíp gắp sticker"` (với từ khóa mục tiêu `"贴纸镊子"`), kiểm tra video trả về đúng loại `content_type=video`, xác thực độ liên quan phức hợp của top kết quả, và chỉ trả về **exit code 0** khi cả hai nền tảng thu thập thành công video thật.
