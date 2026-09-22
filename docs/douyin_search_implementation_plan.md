# Kế Hoạch Triển Khai: Khắc Phục Toàn Diện Các Vấn Đề P0, P1, P2 Cho Bản Nâng Cấp Douyin

Tài liệu này chi tiết hóa phương án khắc phục toàn bộ các lỗi P0, P1, P2 được chỉ ra trong đợt review working tree, đưa hệ thống tìm kiếm Douyin đạt mức **Release-Ready** chuẩn mực.

---

## 1. Phân Tích Nguyên Nhân Gốc & Giải Pháp

### A. Lỗi P0-1: Listener Bỏ Lỡ Response Đầu Tiên Do Gắn Sau `goto()`
- **Nguyên nhân**: `DouyinProvider.search()` gọi `ensure_open(initial_url=direct_url)`. Bên trong `ensure_open()`, trình duyệt đã chạy `page.goto(nav_target)` trước khi hàm trả về. Response listener `page.on("response", ...)` chỉ được gắn sau đó tại dòng 188. Nếu gói JSON API trả về nhanh, listener chưa kịp tồn tại nên `intercepted_results` bị rỗng.
- **Giải pháp**:
  1. Trong `provider.py`: Gọi `self.browser.ensure_open()` **không truyền** `initial_url` (để chỉ mở/lấy trang trắng hoặc trang hiện tại).
  2. Gắn ngay `page.on("response", handle_response)` lên `page`.
  3. Sau đó mới thực hiện `await page.goto(direct_url, ...)`. Đảm bảo 100% request/response của lượt tìm kiếm đều đi qua listener đã kích hoạt.
  4. Hủy listener sạch sẽ trong khối `finally:`.

### B. Lỗi P0-2: Tìm Từ Khóa Thứ Hai Bị Lỗi Thay Vì Điều Hướng
- **Nguyên nhân**: Nếu trình duyệt đang ở một URL tìm kiếm trước đó (ví dụ `/search/decor%20b%C3%A0n%20h%E1%BB%8Dc`), `ensure_open()` trả lại trang đó. Điều kiện `if page.url != direct_url and not unquote(urlsplit(page.url).path).startswith("/search"):` thấy đường dẫn bắt đầu bằng `/search` nên **bỏ qua không gọi `goto()`**. Đến dòng 226–232, code kiểm tra từ khóa trên URL thấy khác query mới ("decor bàn học" != "bjd makeup") và ném `SelectorChangedError`.
- **Giải pháp**:
  1. Loại bỏ logic chặn điều hướng sai lầm `not unquote(urlsplit(page.url).path).startswith("/search")`.
  2. So sánh trực tiếp: Nếu `page.url != direct_url` hoặc từ khóa hiện tại khác từ khóa mới, **luôn luôn điều hướng** `await page.goto(direct_url, wait_until="domcontentloaded")`.
  3. Bổ sung bài unit test mô phỏng 2 lượt tìm kiếm liên tiếp trên cùng 1 page instance, xác nhận điều hướng mượt mà không vấp ngoại lệ.

### C. Lỗi P1-1: Bộ Lọc Thời Gian & Sắp Xếp Chưa Được Đảm Bảo Ở Backend
- **Nguyên nhân**: Tham số `sort_type` và `publish_time` mới chỉ được gắn vào URL web. Nếu Douyin trả về dữ liệu lệch hoặc hệ thống dùng DOM fallback, dữ liệu không được lọc hay sắp xếp lại.
- **Giải pháp**:
  1. **Tại `douyin/parser.py` (`parse_search_response_json`)**:
     - Lọc `publish_time`: Khi `it.create_time` / `it.published_at` có dữ liệu, loại bỏ video nằm ngoài khung giờ (`day` <= 24h, `week` <= 7 ngày, `six_months` <= 182 ngày).
     - Sắp xếp `sort_by`: Sắp xếp mảng kết quả theo `like_count` giảm dần nếu là `most_liked`, hoặc theo `published_at` giảm dần nếu là `newest`.
  2. **Tại `search_service.py` (`persist_results`)**:
     - Thêm lớp phòng thủ cuối cùng (defense-in-depth): Kiểm tra `job_filters` để lọc và sắp xếp trước khi lưu vào database.

### D. Lỗi P1-2: Chưa Có Chức Năng Tải/Chụp Ảnh Carousel (图文)
- **Nguyên nhân**: Mới chỉ lưu URL danh sách ảnh, chưa có cơ chế tải ảnh cục bộ hoặc chụp màn hình `element.screenshot()` khi bị CDN chặn hotlink như mô tả trong báo cáo.
- **Giải pháp**:
  1. Thêm phương thức `download_carousel_images(video, target_dir)` trong `DownloadService` và `DouyinBrowserService`.
  2. Thử tải trực tiếp danh sách ảnh qua HTTP stream với Referer `https://www.douyin.com/`.
  3. Nếu gặp lỗi `403 Forbidden` (chặn hotlink), Playwright điều hướng tới `/note/{id}`, định vị phần tử ảnh trên viewport và dùng `img_element.screenshot(path=...)` để lưu ảnh chất lượng gốc về thư mục tải xuống.

### E. Lỗi P1-3: Parser Carousel Chưa Bao Phủ Các Schema Phổ Biến
- **Nguyên nhân**: Chỉ đọc `aweme_info.images[].url_list`.
- **Giải pháp**:
  Mở rộng `extract_douyin_carousel_images(item: dict) -> list[str]` kiểm tra đầy đủ các schema:
  - `item.get("images")`: `[{"url_list": [...]}, ...]`
  - `item.get("image_post_info", {}).get("images", [])`: `[{"display_image": {"url_list": [...]}}, ...]`
  - `item.get("image_list")`: danh sách ảnh trực tiếp
  - `item.get("images", [])`: kiểm tra `download_url_list` và `download_addr.url_list`
  - Nhận diện `live_photo` clip nếu có video đính kèm trong item ảnh.

### F. Lỗi P1-4: Benchmark "Real Crawl" Cần Dữ Liệu Provenance & Tách Riêng Live Acceptance Test
- **Nguyên nhân**: Mảng mẫu kiểm chuẩn hardcode không có nguồn gốc URL, timestamp hay ID thật.
- **Giải pháp**:
  1. Tạo tập tin `data/benchmarks/crawled_benchmark_samples.json` chứa đầy đủ metadata xuất xứ: `query`, `platform`, `url`, `canonical_url`, `platform_video_id`, `collected_at` (ISO 8601 timestamp), `title`, `author`, `like_count`, `ground_truth_relevance`, `rationale`.
  2. Cập nhật `tests/test_evaluation_benchmark.py` đọc từ file JSON có provenance này.
  3. Tạo `tests/test_real_crawl_acceptance.py` tách riêng bài kiểm thử crawl thật với nhãn `@pytest.mark.live_acceptance`, chỉ chạy khi có cờ kích hoạt hoặc môi trường headless khả dụng.
  4. Cập nhật `scripts/release_gate.py` báo cáo số liệu trung thực, minh bạch.

### G. Lỗi P1-5: Khắc Phục Flakiness Trong `test_download_queue.py`
- **Nguyên nhân**: Vòng lặp chờ job hoàn thành `for _ in range(30): await asyncio.sleep(0.05)` chỉ tối đa 1.5 giây. Khi chạy full suite 360 test, CPU bận khiến quá 1.5s chưa xong, câu lệnh `await service.shutdown()` tiếp theo đã ép hủy job thành `cancelled`.
- **Giải pháp**:
  1. Nâng vòng lặp chờ lên `range(120)` (6.0 giây) hoặc đợi đến khi trạng thái thuộc terminal (`{"completed", "failed", "cancelled"}`).
  2. Đảm bảo `service.shutdown()` cho phép worker hoàn tất tác vụ hiện tại một cách êm thuận (graceful drain).
  3. Chạy kiểm thử full suite lặp lại ít nhất 3 lần liên tiếp để chứng minh loại bỏ 100% flakiness.

### H. Lỗi P2-1: SQLite Migration Thất Bại Không Được Nuốt Lỗi
- **Nguyên nhân**: `except Exception: pass` tại `backend/app/main.py:87`.
- **Giải pháp**:
  1. Xóa bỏ `pass`.
  2. Ghi log `logger.exception("Database migration failed: %s", e)`.
  3. Thêm bước kiểm tra PRAGMA xác thực tất cả các cột bắt buộc (`media_type`, `images`, `filters`) đã tồn tại; nếu thiếu thì báo lỗi ngăn chặn khởi động sai.

### I. Lỗi P2-2: Giao Diện Người Dùng Chỉ Hiển Thị Filter Cho Nền Tảng Hỗ Trợ
- **Nguyên nhân**: UI hiển thị filter `Sắp xếp`, `Thời gian`, `Định dạng` cho cả Xiaohongshu và mock dù các nền tảng này chưa hỗ trợ.
- **Giải pháp**:
  Trong `frontend/features/search/search-workspace.tsx`:
  - Chỉ render nhóm filter nâng cao khi `mode === "douyin"`.
  - Khi ở chế độ khác, ẩn đi hoặc hiển thị ghi chú rõ ràng chỉ áp dụng cho Douyin thật.

### J. Đồng Bộ Tài Liệu Vào Kho Codebase
- Sao chép `douyin_search_research_report.md` và `implementation_plan.md` vào thư mục `docs/` của dự án để đảm bảo tài liệu được phiên bản hóa đầy đủ cùng mã nguồn.

---

## 2. Danh Sách Tập Tin Đã Thay Đổi

### Core Backend
- `backend/providers/douyin/provider.py`: Sửa thứ tự gắn response listener trước `goto()`, sửa điều hướng từ khóa thứ 2.
- `backend/providers/douyin/parser.py`: Mở rộng parser hỗ trợ 5 schema carousel ảnh + lọc/sắp xếp backend.
- `backend/services/search_service.py`: Bổ sung lớp phòng thủ post-filtering & sorting theo `job_filters`.
- `backend/services/download/service.py`: Tích hợp `_download_image_album` và fallback Playwright screenshot.
- `backend/services/douyin_browser_service.py`: Hỗ trợ phương thức chụp ảnh phần tử `screenshot_note_images()`.
- `backend/app/main.py`: Sửa xử lý migration SQLite, ghi log và kiểm tra PRAGMA nghiêm ngặt.

### Frontend
- `frontend/features/search/search-workspace.tsx`: Chỉ hiển thị filter đa chiều khi chọn Douyin thật.

### Dữ Liệu & Kiểm Thử
- `data/benchmarks/crawled_benchmark_samples.json`: [MỚI] Bộ dữ liệu benchmark chuẩn có provenance rõ ràng.
- `tests/test_douyin_hybrid_search.py`: Bổ sung test bắt response đầu tiên, test 2 lượt tìm liên tiếp, test nhiều schema carousel, test post-filtering.
- `tests/test_download_queue.py`: Tăng timeout chờ job và khử triệt để race condition.
- `tests/test_evaluation_benchmark.py`: Tải dữ liệu từ fixture provenance JSON.
- `tests/test_real_crawl_acceptance.py`: [MỚI] Acceptance test crawl thật tách riêng.
- `scripts/release_gate.py`: Cập nhật logic benchmark trung thực, thêm kiểm tra Playwright E2E và git clean working tree.

### Tài Liệu
- `docs/douyin_search_research_report.md`: [MỚI] Lưu trữ báo cáo nghiên cứu trong repo.
- `docs/douyin_search_implementation_plan.md`: [MỚI] Lưu trữ kế hoạch kỹ thuật trong repo.

---

## 3. Kế Hoạch Kiểm Chứng & Nghiệm Thu

1. **Unit Tests Chi Tiết**:
   - `test_douyin_hybrid_search.py`: Chạy độc lập kiểm tra listener timing, 2 sequential searches, carousel schemas, defense filtering.
   - `test_download_queue.py`: Khử hoàn toàn flakiness.
2. **Full Suite Pytest Regression**:
   - Chạy toàn bộ test suite 3 lần liên tiếp:
     `pytest tests/ -q` (yêu cầu 100% pass ở cả 3 lượt).
3. **Static Quality Checks**:
   - `ruff check .` (0 errors)
   - `mypy backend` (0 errors)
   - `npm run lint` (0 errors/warnings)
   - `npx tsc --noEmit` (0 errors)
4. **Next.js Production Build**:
   - `npm run build` (Biên dịch thành công 9/9 route).
5. **Release Gate**:
   - Chạy `python scripts/release_gate.py` đảm bảo đạt `APPROVED (GREEN)`.
