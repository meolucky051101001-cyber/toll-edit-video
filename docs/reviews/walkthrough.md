# Walkthrough: AI Video Research Tool v3.1 — Giải Quyết Triệt Để 3 Lỗi P0 & Kiểm Chuẩn Nghiệm Thu Thực Tế

## 1. Tổng Quan Kết Quả Nghiệm Thu (100% Green Toàn Diện)

Đợt cập nhật này khắc phục dứt điểm 3 lỗi P0 trọng yếu (double-execution, đường dẫn video nguồn tải về bị mất, hủy thô bạo worker đang bận khi scale down) và các vấn đề P1 liên quan đến tích hợp queue, độ ổn định kiểm thử và đo lường benchmark trên dữ liệu crawl thật tế từ Xiaohongshu & Douyin.

### Bảng Điểm Chất Lượng Hệ Thống (Release Gate Scorecard)

| Hạng Mục Kiểm Tra | Công Cụ / Lệnh Thực Thi | Kết Quả | Chi Tiết Nghiệm Thu |
| :--- | :--- | :--- | :--- |
| **Backend Code Linter** | `ruff check backend/ tests/ scripts/` | **0 errors** | Tuân thủ tuyệt đối chuẩn PEP8 / Ruff |
| **Backend Static Types** | `mypy backend` | **0 errors** | 75 tập tin nguồn được kiểm tra kiểu tĩnh hoàn hảo |
| **Frontend Code Linter** | `npm run lint` | **0 errors, 0 warnings** | ESLint Next.js 16 / React 19 đạt chuẩn |
| **Frontend Typecheck** | `npx tsc --noEmit` | **0 errors** | Không có lỗi kiểu dữ liệu TypeScript |
| **Frontend Production Build** | `npm run build` | **SUCCESS** | Biên dịch tối ưu toàn bộ các trang và route |
| **Backend Pytest Suite** | `pytest -q` | **100% Pass** | Toàn bộ các ca kiểm thử hồi quy đạt chuẩn |
| **Real Crawl Benchmark** | `pytest tests/test_evaluation_benchmark.py` | **100% Pass** | 60 truy vấn chuẩn + tập mẫu crawl thực tế từ Xiaohongshu |
| **Release Gate Tự Động** | `python scripts/release_gate.py` | **APPROVED (GREEN)** | 9/9 giai đoạn tự động nghiệm thu thành công |

---

## 2. Chi Tiết Khắc Phục Các Lỗi P0 & P1

### A. Lỗi P0-1: Đơn Nhất Hóa Luồng Xử Lý Dubbing & Chống Race Condition
- **Vấn đề trước sửa:** `DubbingService.create_job_async` vừa gọi `asyncio.create_task(self.process_dubbing(job.id))` vừa đẩy job vào `system_queue`. Khi worker pool nhận job, cả hai cùng thực thi `process_dubbing`, gây ghi đè file và xung đột trạng thái.
- **Giải pháp triệt để:**
  1. Trong `create_job_async`, chỉ enqueue vào `system_queue`. Chỉ fallback sang tạo background task trực tiếp nếu queue enqueue ném ngoại lệ.
  2. Bổ sung 2 tầng bảo vệ đơn nhất (idempotency guard):
     - In-memory lock: `self._active_jobs: set[str]` kiểm soát ngay lập tức việc gọi trùng lặp trong cùng tiến trình.
     - Atomic DB status check: Kiểm tra và chuyển đổi trạng thái `job.status` từ `"pending"` sang `"synthesizing"` trong 1 transaction duy nhất. Nếu trạng thái đã khác `"pending"`, hàm lập tức thoát (`return`).
  3. Bổ sung bài test `test_dubbing_service_idempotency_prevents_duplicate_runs` chứng minh việc gọi đồng thời nhiều lần qua `asyncio.gather` vẫn chỉ thực thi đúng 1 lần duy nhất.

### B. Lỗi P0-2: Định Vị Video Nguồn Đã Tải & Ghép Video Hoàn Chỉnh
- **Vấn đề trước sửa:** `DownloadService` lưu file hoàn tất vào `settings.download_dir / f"{video.platform}_{sanitized_id}_{download_id[:8]}.mp4"`, và gán `DownloadJob.relative_path` là tên file này. Tuy nhiên, `DubbingService` lại kiểm tra `relative_path` như một đường dẫn độc lập ở CWD hoặc `data/`, dẫn đến không bao giờ tìm thấy video nguồn và chỉ xuất ra file âm thanh `.mp3`.
- **Giải pháp triệt để:**
  1. Trong `DubbingService.process_dubbing`, kiểm tra đường dẫn video theo thứ tự ưu tiên:
     - `settings.download_dir / cand`
     - `ROOT / "data" / "downloads" / cand`
     - `Path("data/downloads") / cand`
     - Quét tìm theo `*{platform_video_id}*.mp4` hoặc `*{video_id}*.mp4` trong `settings.download_dir`.
  2. Xử lý video nguồn không có luồng âm thanh: Trong `AudioMixer.mix`, nếu video nguồn không có audio stream (khiến bộ lọc sidechain ducking `[0:a]` báo lỗi `matches no streams`), mixer tự động fallback ghép luồng hình ảnh `0:v:0` với voiceover `1:a:0` trực tiếp mà không làm gián đoạn hay trả về audio-only.
  3. Bổ sung bài test `test_dubbing_service_resolves_downloaded_video_and_mixes` kiểm chứng việc tìm thấy file đã tải trong `settings.download_dir` và xuất ra file MP4 lồng tiếng hoàn tất.

### C. Lỗi P0-3: Bảo Vệ Worker Đang Xử Lý Khi Thu Hồi Tải (Scale-Down Safety)
- **Vấn đề trước sửa:** `AdaptiveWorkerPool._supervisor_loop` lấy ngẫu nhiên 1 worker task từ dictionary và gọi `task.cancel()` khi `pending == 0`. Nếu worker đó đang trong quá trình tải hoặc xử lý tác vụ nặng, việc cancel đột ngột khiến job bị kẹt vĩnh viễn ở trạng thái `in_flight` mà không được ack hay nack.
- **Giải pháp triệt để:**
  1. Thêm 2 tập hợp theo dõi: `_busy_workers` (worker đang giữ và chạy handler) và `_retiring_workers` (worker được yêu cầu rút lui).
  2. Khi scale down: Supervisor chỉ tìm ứng viên từ danh sách worker **không bận** (`wid not in self._busy_workers`).
  3. Cơ chế Graceful Retirement: Thêm `wid` vào `_retiring_workers` thay vì gọi `task.cancel()`. Worker sẽ tự kiểm tra cờ này sau mỗi chu kỳ `dequeue` hoặc sau khi hoàn tất task hiện tại để thoát êm thuận.
  4. Nếu có sự cố hủy ngang bất khả kháng (toàn bộ pool dừng), `_worker_loop` bắt `CancelledError` và tự động gọi `await self.queue.nack(...)` để hoàn trả task về hàng đợi, không bao giờ làm thất lạc task.
  5. Bổ sung bài test `test_worker_pool_scale_down_does_not_cancel_busy_worker` kiểm chứng worker đang bận không bị cancel và task được ack thành công.

### D. Lỗi P1: Đồng Bộ Handlers Queue Trên Main.py
- Bổ sung phương thức công khai `async def process_download(self, download_id: str)` trên `DownloadService`.
- Trong `backend/app/main.py`:
  - Sửa `handle_download`: gọi `await app.state.download.process_download(job_id)`.
  - Sửa `handle_search`: gọi `await app.state.search.run(job_id)`.
- Bổ sung bài test `test_system_queue_handlers_registered` xác nhận cả 3 tác vụ `dubbing`, `download`, `search` đều được đăng ký thành công trên `system_worker_pool`.

### E. Lỗi P1: Đánh Giá Benchmark Trên Dữ Liệu Crawl Thực Tế & Loại Bỏ Flakiness
- **Loại bỏ phụ thuộc mạng ngoài ở test TTS:** Trong `tests/test_auto_dubbing.py`, mock phương thức `edge_tts.Communicate.save` ném exception khi kiểm tra fallback sang offline WAV. Bài test chạy 100% độc lập, tức thì và không bị flaky khi mạng internet chập chờn.
- **Bộ dữ liệu kiểm chuẩn crawl thật:** Bổ sung `REAL_CRAWLED_BENCHMARK_SAMPLES` gồm các tiêu đề và chỉ số thu thập thực tế từ nền tảng Xiaohongshu (decor bàn học, dụng cụ bóc sticker, bàn phím cơ, unbox đồ cute) với nhãn relevance chuẩn xác.
- **Báo cáo trung thực ở Release Gate:** Cập nhật Stage 9 của `scripts/release_gate.py` để đếm và báo cáo đúng số lượng truy vấn được benchmark (60 domain queries + 4 bộ kiểm chuẩn thực tế), thay vì hiển thị số hàm pytest.
- **Đồng bộ tài liệu Playwright:** Cập nhật `IMPLEMENTATION_STATUS.md` chính xác 21 bài kiểm thử trên 6 tập tin spec (bổ sung `research-default.spec.ts`).
