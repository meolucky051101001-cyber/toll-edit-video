# Walkthrough: AI Video Research Tool — Báo Cáo Nghiệm Thu 11 Giai Đoạn & Đánh Giá Vận Hành Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (Comprehensive 11-Stage Scorecard)

Hệ thống đã trải qua quy trình kiểm chuẩn tự động nghiêm ngặt gồm 11 giai đoạn thông qua công cụ cổng phát hành `scripts/release_gate.py --live --require-live`. Toàn bộ các giai đoạn kiểm thử tĩnh, kiểm thử đơn vị, kiểm thử tích hợp điều phối SQLite, benchmark dữ liệu fixture hồi quy có xác minh nguồn gốc nghiêm ngặt và E2E đều đạt chuẩn **PASS (0 lỗi)**. Giai đoạn 11 (Live Crawl & Reachability) phản ánh trung thực khả năng kết nối mạng thực tế với chứng thực mã trạng thái HTTP trực tiếp, kiểm thử fail-closed đồng thời, và phân định rõ ràng giữa môi trường vận hành trạm làm việc (Workstation Operational) với yêu cầu thu thập video trực tiếp không cần đăng nhập/xác minh.

### Bảng Điểm Kiểm Chuẩn Toàn Diện (Release Gate Scorecard)

| STT | Giai Đoạn Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :---: | :--- | :--- | :--- | :--- |
| **1** | **Runtime Environment** | `check_environment()` | **PASS** | Python 3.12.14 verified (>= 3.11) [0.0s] |
| **2** | **Database Schema** | `check_database()` | **PASS** | 5 bảng cốt lõi SQLite (`videos`, `search_jobs`, `job_videos`, `dubbing_jobs`, `video_metric_snapshots`) [0.27s] |
| **3** | **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff [0.08s] |
| **4** | **Backend Static Types** | `mypy backend` | **0 errors** | 76/76 tập tin nguồn backend kiểm tra kiểu tĩnh hoàn hảo [0.34s] |
| **5** | **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn [5.44s] |
| **6** | **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | 0 lỗi TypeScript trên toàn bộ codebase frontend [1.99s] |
| **7** | **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ trang và API routes Next.js [34.39s] |
| **8** | **Backend Pytest Suite** | `pytest -q` | **462/462 PASS** | 462 passed, 0 failed, 3 skipped [144.67s] |
| **9** | **Fixture & Crawled Benchmark** | `pytest tests/test_evaluation_benchmark.py tests/test_query_precision_evaluation.py ...` | **PASS** | 60 domain queries + 4 crawled queries [20 crawled + 13 holdout samples: 16 video, 4 img/txt; 33/33 strict provenance verified `strict_url_id_iso_parsed_with_evidence`] [2.46s] |
| **10** | **Playwright E2E Tests** | `npx playwright test` | **26/26 PASS** | 26 ca kiểm thử E2E trên 7 tập tin spec (bao gồm ca kiểm thử điều phối `active_platform` đa nền tảng) [39.0s] |
| **11** | **Live Crawl Acceptance** | `python scripts/release_gate.py --live --require-live` | **OPERATIONAL** | Network Reachability: **PASS** (Douyin: HTTP 200, XHS: HTTP 200). Retrieval: Douyin BLOCKED (CHALLENGE_REQUIRED), XHS BLOCKED (LOGIN_REQUIRED) [25.55s] |

**Phán quyết Cổng Phát hành (Verdict)**:  
`APPROVED (WORKSTATION READY) - LIVE RETRIEVAL BLOCKED BY AUTH` (Tổng thời gian: 259.31s)  
*Chi tiết kiểm chứng đồng bộ tuyệt đối lưu tại tệp versioned:* [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json).

---

## 2. Tiêu Chuẩn Thực Thi Lệnh & Chế Độ Nghiêm Ngặt (Strict Gate)

Bằng chứng thực thi Strict Gate đã được ghi nhận trực tiếp vào [`release_gate_report.json`](file:///C:/Users/admin/Projects/ai-video-research-tool/release_gate_report.json) với `executed: true`, `exit_code: 2`, và chi tiết cấu trúc nền tảng phân tách minh bạch:

```json
"strict_live_run": {
  "executed": true,
  "command": "python scripts/release_gate.py --live --require-live",
  "exit_code": 2,
  "timestamp": "2026-09-24T08:59:54Z",
  "query": "nhíp gắp sticker",
  "submitted_query": "贴纸镊子",
  "douyin": {
    "status": "BLOCKED (CHALLENGE_REQUIRED)",
    "network_reachable": true,
    "browser_reachable": true,
    "retrieval_available": false,
    "retrieval_block_reason": "CAPTCHA_CHALLENGE",
    "precision_verified": false,
    "results_count": 0,
    "reason": "CAPTCHA or slider challenge encountered during live search"
  },
  "xiaohongshu": {
    "status": "BLOCKED (LOGIN_REQUIRED)",
    "network_reachable": true,
    "browser_reachable": true,
    "retrieval_available": false,
    "retrieval_block_reason": "LOGIN_REQUIRED",
    "precision_verified": false,
    "results_count": 0,
    "reason": "User login session required to view search results on workstation"
  }
}
```

| Mục Đích Kiểm Thử | Lệnh Tiêu Chuẩn | Hành Vi & Phán Quyết | Mã Thoát (Exit Code) Thực Tế |
| :--- | :--- | :--- | :---: |
| **Kiểm thử nghiêm ngặt thu thập thật (Strict Gate)** | `python scripts/release_gate.py --live --require-live` | Bắt buộc cả Douyin và XHS phải trả về `>= 3 video thật` đạt tỷ lệ liên quan `>= 80%`. Khi phiên live chưa tự giải CAPTCHA / đăng nhập trên trạm làm việc, cổng phát hành từ chối và thoát mã lỗi 2 (fail-closed trung thực). | `2` (fail-closed, không báo FULL GREEN ảo) |
| **Nghiệm thu toàn bộ 11 Stage (Vận hành máy trạm)** | `python scripts/release_gate.py --live` | Chạy toàn bộ 11 stage. Ở Stage 11, kiểm tra Chromium thật mở được Douyin và XHS (HTTP 200). Khi gặp CAPTCHA/Login, hệ thống ghi nhận đúng trạng thái và kết luận `APPROVED (WORKSTATION READY)`. | `0` |
| **Kiểm thử hồi quy backend** | `pytest -q` | Chạy toàn bộ 462 bài kiểm thử tự động, 3 bài test live crawl được bỏ qua an toàn khi chưa bật `LIVE_CRAWL_ENABLED=1`. | `0` |
| **Chạy cổng phát hành offline (10 Stage)** | `python scripts/release_gate.py` | Chạy đầy đủ từ Stage 1 đến Stage 10 (bỏ qua Stage 11 Live Crawl). | `0` |

---

## 3. Phân Định Minh Bạch Các Tầng Kiểm Thử & Tiêu Chí Độ Chính Xác Mới

### A. Tách Kiểm Tra Cấu Trúc Khỏi Xác Minh Nguồn Gốc (Decoupled Provenance Audit)
- **Hàm kiểm định**: [`audit_sample_provenance(sample, expected_query=None)`](file:///C:/Users/admin/Projects/ai-video-research-tool/backend/services/identity_policy.py#L510-L702).
- **Kết quả trả về**: Đối tượng `SampleProvenanceResult` bao gồm các trường riêng biệt:
  - `schema_valid`: `True` nếu mẫu thỏa mãn schema cơ bản (ID hợp lệ, URL canonical, ISO 8601 có timezone, media_type video/image_text, title, author, ground_truth_relevance boolean).
  - `source_provenance_verified`: `True` chỉ khi nguồn gốc capture được xác minh đầy đủ metadata và bằng chứng phiên.
  - `status`: Phân loại rõ ràng: `VERIFIED_STRICT`, `UNVERIFIED_LEGACY`, `UNVERIFIED_MISSING_METADATA`, `UNVERIFIED_INSUFFICIENT_EVIDENCE`, hoặc `INVALID_SCHEMA`.
  - `reasons`: Danh sách chi tiết các lỗi từ chối nếu có.
- **Quy tắc an ninh nguồn gốc nghiêm ngặt**:
  - Chống giả mạo Hostname: Kiểm tra chính xác danh sách host tin cậy (`host.lower() in ("www.douyin.com", "douyin.com")`), từ chối triệt để tên miền giả mạo như `douyin.com.attacker.test` hoặc `evil-douyin.com`.
  - Đối chiếu ID trích xuất từ canonical regex với `platform_video_id`.
  - Parse `collected_at` bằng `datetime.fromisoformat()`, từ chối timestamp không hợp lệ hoặc thiếu thông tin múi giờ (naive).
  - Mẫu `legacy_unverified` có `schema_valid=True` nhưng tuyệt đối **không** được tính vào `source_provenance_verified`.
  - Kiểm tra metadata bắt buộc: `query`, `submitted_query`, `session_id`, `collector_version`, `provenance_source`, `provenance_level == "verified_strict_audit"`.
  - Đối chiếu metadata `query` của mẫu với `expected_query` của nhóm bên ngoài.
  - Yêu cầu bằng chứng nguồn gốc: Session ID định dạng chuẩn hoặc `source_capture_ref` / `capture_id` / `metadata_hash`.
- **Bộ test chuyên biệt**: [`tests/test_provenance_audit.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_provenance_audit.py) gồm 8 ca kiểm thử bao phủ toàn diện các trường hợp biên và tấn công giả mạo.

### B. Khóa Chặt Cấu Hình Live Gate (Gate Config Lockdown)
- **Hàm xác thực**: [`validate_live_gate_config(min_samples, min_relevance_rate)`](file:///C:/Users/admin/Projects/ai-video-research-tool/backend/services/ranking_service.py#L472-L513).
- **Quy tắc biên giới**:
  - `min_samples` bắt buộc là số nguyên $\ge 3$.
  - `min_relevance_rate` bắt buộc là số thực trong khoảng $(0.0, 1.0]$, nghiêm cấm `NaN`, vô cực (`Inf`), hoặc giá trị ngoài biên.
  - Phát sinh lỗi `ValueError` kèm thông báo giải thích rõ ràng nếu vi phạm.
- **Khởi động cổng phát hành**: `scripts/release_gate.py` gọi kiểm tra `validate_live_gate_config` ngay ở preflight check, hủy bỏ ngay lập tức nếu cấu hình bất thường.
- **Bộ test chuyên biệt**: [`tests/test_live_gate_config.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_live_gate_config.py) gồm 3 ca kiểm thử xác nhận từ chối các giá trị sai lệch.

### C. Phân Tách Minh Bạch Chỉ Số & Khái Niệm Dataset (Regression Fixture Benchmark)
- **Định danh minh bạch**: Toàn bộ tập dữ liệu mẫu nội bộ tại `data/benchmarks/independent_holdout_benchmark.json` được định danh chuẩn xác là **`REGRESSION FIXTURE`**, không đánh tráo khái niệm là dữ liệu kiểm chuẩn độc lập bên ngoài khi chưa có protocol dán nhãn bên thứ ba.
- **Không gộp chung chỉ số**: Phân tách rõ ràng kết quả thành 3 bộ đo chuyên biệt:
  1. **Top-k Ranking Precision (Precision@k)**: Đánh giá độ chính xác xếp hạng cho từng truy vấn đa item và độ chính xác bình quân Mean Precision@k = 1.00.
  2. **Compound-Intent Validator**: Đo lường riêng cho truy vấn ghép `"nhíp gắp sticker"` ($N=5$ Douyin samples, Precision = 1.00, Accuracy = 1.00, phân loại chính xác tất cả các distractor).
  3. **General Threshold Classifier**: Đo lường ngưỡng phân loại ($Score \ge 15.0$) cho 4 truy vấn lĩnh vực khác ($N=8$, Precision = 1.00, Accuracy = 1.00).
- **Báo cáo phân tách**: Báo cáo rõ ràng số lượng mẫu ($N$), nền tảng (`Douyin only (Xiaohongshu: PENDING DATA)`), và định dạng (`video` vs `image_text`).

### D. Phạm Vi Kiểm Định Truy Vấn Ghép & Phòng Thủ Nhiễu (Compound Intent Scoping & Distractor Defense)
- **Giới hạn phạm vi**: [`validate_compound_intent_relevance()`](file:///C:/Users/admin/Projects/ai-video-research-tool/backend/services/ranking_service.py) và [`validate_sticker_tweezer_compound_intent()`](file:///C:/Users/admin/Projects/ai-video-research-tool/backend/services/ranking_service.py) giới hạn nghiêm ngặt cho truy vấn `"nhíp gắp sticker"`; tự động ném `ValueError` nếu truyền truy vấn không hỗ trợ.
- **Tiêu chí kép Tool VÀ Purpose**:
  - **Tool (Dụng cụ)**: `镊子`, `鑷子`, `nhíp`, `nhip`, `nhíp gắp`, `kẹp gắp`, `tweezers`, `tweezer`.
  - **Purpose (Mục đích)**: `贴纸`, `貼紙`, `咕卡`, `sticker`, `stickers`, `hình dán`, `guka`.
  - `手账` (sổ tay/planner) đứng riêng **tuyệt đối không được tính là sticker**.
- **Chống spam thẻ/hashtag**: Video hợp lệ bắt buộc phải chứa từ khóa thực thể trong `title` (tiêu đề), loại bỏ các video nấu ăn/vlog chỉ gắn tag `#贴纸 #镊子` ở caption/hashtag.
- **Loại trừ lĩnh vực không tương thích (Incompatible Domain Rejection)**: Tự động từ chối nhíp nối mi (`睫毛`), nhíp làm đẹp mắt (`双眼皮`), hoặc nhíp sửa điện thoại/điện tử (`修手机`, `主板`).
- **Bộ test chuyên biệt**: [`tests/test_compound_intent_distractors.py`](file:///C:/Users/admin/Projects/ai-video-research-tool/tests/test_compound_intent_distractors.py) gồm 8 ca kiểm thử xác nhận phân loại chính xác các loại distractor.

### E. Minh Bạch Trạng Thái Vận Hành Live (Live Operational Transparency)
- Loại bỏ hoàn toàn trường `crawl_operational` chung chung. Phân tách thành 5 chỉ số rõ ràng:
  - `network_reachable`: Máy chủ phản hồi HTTP probe thành công (`HTTP 200`).
  - `browser_reachable`: Trình duyệt Chromium mở được trang trên máy trạm.
  - `retrieval_available`: Tìm kiếm live trả về kết quả thật mà không bị chặn bởi cơ chế bảo vệ.
  - `retrieval_block_reason`: Ghi rõ lý do chặn cụ thể (`CAPTCHA_CHALLENGE` hoặc `LOGIN_REQUIRED`).
  - `precision_verified`: Độ chính xác của kết quả live được xác minh bằng validator.
- Khi bị chặn: `results_count = 0`, `relevant_count = 0`, `precision_verified = false`, `retrieval_available = false`.
- Lệnh `python scripts/release_gate.py --live --require-live` bắt buộc trả về **exit code 2** (fail-closed trung thực).

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
   - Hệ thống sẽ tìm kiếm từ khóa `"nhíp gắp sticker"` (với từ khóa mục tiêu `"贴纸镊子"`), kiểm tra video trả về đúng loại `content_type=video`, xác thực độ liên quan phức hợp của top kết quả theo quy chuẩn $\ge 3$ video và $\ge 80\%$ relevance rate, và chỉ trả về **exit code 0** khi cả hai nền tảng thu thập thành công video thật.
