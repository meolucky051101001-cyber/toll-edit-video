# Xiaohongshu: kiểm tra cải thiện tìm kiếm — 27/09/2026

## Kết quả vận hành

Ứng dụng đang chạy tại `http://localhost:3000` (backend `/api/health` OK, frontend HTTP 200). Cùng truy vấn **dụng cụ bóc sticker**, `limit=20`, `use_ai=false`, `content_type=video`, phiên Xiaohongshu đã đăng nhập:

| Phiên bản | Job ID | Truy vấn gửi nền tảng | Video lưu / 20 | Ứng viên xử lý | Bị lọc | Thời gian |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Trước sửa | `e19cd275-3fca-4267-9b1a-d620d04b1b35` | 贴纸镊子 / 夹取贴纸 / 手账夹取贴纸 | 2 | 168 | 164 | 52.3 s |
| Đọc mô tả chi tiết | `417a436c-8606-40cb-bfbd-b12501511aa1` | như trên | 9 | 27 | 17 | 100.9 s |
| Thêm thuật ngữ bóc sticker | `90fb3dbc-8158-4ec2-ac53-c580fe44b4b2` | 贴纸镊子 / 夹取贴纸 / 贴纸剥离器 | **11** | 31 | 18 | 117.7 s |

Cả 11 mục của lượt cuối đều có `is_mock=false`, `media_type=video`, ID và URL bài hợp lệ theo API. Số ứng viên xử lý có thể bao gồm bài lặp giữa các truy vấn; chỉ số 11 là số video duy nhất gắn với job. Không có CAPTCHA/lỗi đăng nhập trong ba lượt này. Đây là kiểm tra metadata và URL, chưa đánh giá từng khung hình hoặc độ chính xác trên nhiều chủ đề. Tool **chưa đạt chỉ tiêu 20/20** và chưa được nghiệm thu tìm hàng loạt ổn định.

## Kiểm tra truy vấn chính xác “nhíp gắp sticker”

Đã nối validator ý định ghép vào đường lưu kết quả thật: video chỉ có “nhíp” trong tiêu đề rồi gắn `#贴纸` ở mô tả bị loại; bài ảnh cũng không thể vượt video trong ranker. Lượt thử `limit=10`, `use_ai=false`, chỉ Xiaohongshu:

| Cấu hình | Job ID | Video lưu / 10 | Ứng viên | Bị lọc | Thời gian |
| --- | --- | ---: | ---: | ---: | ---: |
| Trước đổi bộ từ khóa, đã bật validator chặt | `a995951e-7dee-4333-af99-2a2321e0f011` | 1 | 17 | 15 | 54.9 s |
| Cấu hình mặc định cuối: ưu tiên `造景贴纸镊子` | `ad052c31-6e1a-4c95-ba74-23a68486f7b9` | **5** | 14 | 9 | 53.9 s |

Cả 5 tiêu đề ở lượt cuối chứa rõ nhíp và sticker; API ghi `media_type=video`. Thử nghiệm riêng một truy vấn `造景贴纸镊子` (`91055f35-a107-474a-9faa-980d5e0eb6c3`) cũng cho 5/10. Bộ từ khóa mặc định cuối là `造景贴纸镊子`, `贴纸镊子`, `咕卡贴纸镊子`. Vẫn còn thiếu 5 video so với hạn mức 10; đây là lựa chọn giữ độ chính xác theo quy tắc người dùng đã yêu cầu, không phải lỗi giả vờ đủ số lượng.

## Thay đổi

- Chỉ mở tối đa 12 trang chi tiết mỗi truy vấn cho thẻ video có tiêu đề khớp một phần từ khóa; lấy mô tả thật để bộ xếp hạng có đủ ngữ cảnh. Thẻ không có bằng chứng từ khóa trong tiêu đề không chiếm hạn mức ứng viên của provider ở truy vấn ghép tiếng Trung.
- Ưu tiên thẻ có cụm từ hiếm hơn trong kết quả và cache trang chi tiết đã xác minh theo note ID trong cùng job.
- Không dùng mô tả nếu trang chi tiết chuyển hướng về đề xuất hoặc sang note ID khác. Không tạo kết quả thay thế khi trang chi tiết không xác minh được.
- Từ điển nhận diện `贴纸剥离器` / `剥离器` là dụng cụ bóc sticker; truy vấn thứ ba của cụm này đổi sang `贴纸剥离器`. Video chỉ có sticker mà không có dụng cụ vẫn bị loại.
- Với truy vấn đúng cụm “nhíp gắp sticker”, validator tiêu đề/loại video của benchmark giờ chạy cả trong `SearchService.persist_results`; ranker chặn bài ảnh cùng cụm này. Từ khóa cụ thể hiệu quả hơn trên phiên thật được ưu tiên trước.

## Kiểm thử

- `python -m pytest tests/test_sticker_tool_intent.py tests/test_query_precision_evaluation.py tests/test_compound_intent_distractors.py -q -p no:cacheprovider --basetemp <temp>`: **29 passed**, exit 0 sau bản sửa cuối.
- `python -m mypy backend --no-incremental --cache-dir <temp>`: 0 lỗi trên 76 file, exit 0.
- `python -m ruff check --no-cache backend tests scripts`: 0 lỗi, exit 0.
- `python -m pytest -q -p no:cacheprovider -o faulthandler_timeout=45 --basetemp <temp> --tb=short`: **513 passed, 3 skipped, 0 failed**, exit 0, 138.61 s. Log cuối: `logs/codex-regression-search-final-20260927.log`.
- SHA-256 manifest vận hành trước/sau full suite cuối đều là `E474ECB7F963128CECAC6C7F5B8FFFA93FB5976E32BCE1E449447A275E90F0A9`; test không thêm dòng vào manifest vận hành. Những lượt crawl thật trước đó có ghi nối tiếp manifest theo luồng ứng dụng.

Ba test live trong suite bị skip, không được tính là live crawl pass. Không chạy lại full release gate/Playwright E2E ở đợt này, nên báo cáo gate cũ không chứng nhận thay đổi mới.

## Giới hạn và bước nghiệm thu tiếp

Độ bao phủ còn thiếu 9 video cho lượt yêu cầu 20 ở truy vấn “dụng cụ bóc sticker”, và thiếu 5/10 ở truy vấn “nhíp gắp sticker”. Cần đánh giá thủ công 11 kết quả về đúng động tác bóc/gắp sticker, thêm các truy vấn độc lập có chủ đích nếu cần, và chạy vài chủ đề khác cùng lượt 50/100 trên phiên thật trước khi công bố tìm kiếm hàng loạt ổn định. Không hạ ngưỡng chỉ để đủ số lượng.
