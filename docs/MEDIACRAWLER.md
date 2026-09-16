# Tham khảo MediaCrawler và adapter nhập metadata

Ngày đối chiếu: 2026-09-04. Repository: https://github.com/NanmiCoder/MediaCrawler
Commit đã đọc: `d6f7c5bb906b6dac40ddf343ef9e26438a3de092` (2026-08-14).

## Kết luận áp dụng

Tool có adapter độc lập đọc file content JSON/JSONL Xiaohongshu và Douyin theo định dạng xuất của MediaCrawler. Có xem trước, kiểm tra từng bản ghi, loại trùng, lưu SQLite và nguồn nhập. Tính năng không khởi động crawler, không gọi Gemini, không tải video. Chưa có file xuất thật do người dùng cung cấp để nghiệm thu nguồn dữ liệu thực tế; kiểm thử dùng fixture tự tạo trong môi trường tạm.

MediaCrawler hiện dùng NON-COMMERCIAL LEARNING LICENSE 1.1: phạm vi sử dụng/copy/modify/merge cho học tập phi thương mại; dùng thương mại cần đồng ý bằng văn bản của chủ sở hữu. Adapter này được viết riêng từ định dạng trường dữ liệu, không chép/vendoring/cài dependency hoặc chạy mã MediaCrawler. Không coi public repository đồng nghĩa giấy phép thương mại không hạn chế.

Nguồn giấy phép: https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/LICENSE

## Những phần đã đọc và quyết định

| Phần nguồn tham khảo | Điều hữu ích | Áp dụng vào tool |
|---|---|---|
| base/base_crawler.py | Tách crawler, login, store, API client | Giữ SearchProvider/AIProvider và tách importer khỏi search worker |
| media_platform/xhs/core.py | Keyword loop, budget, giới hạn đồng thời, detail rồi store | Giữ budget thấp và pipeline normalize/dedup/rank/SQLite; chưa ghép client API vào ứng dụng |
| media_platform/xhs/login.py | Quản lý vòng đời đăng nhập với browser | Phase 3 sẽ có profile riêng, đăng nhập thủ công và waiting/resume theo master plan |
| media_platform/xhs/exception.py | Phân biệt lỗi truy cập, giới hạn và bài không còn | Giữ các error class provider hiện có; bổ sung tình huống kiểm chứng khi có browser thật |
| store/xhs/__init__.py | Schema content, loại video, số liệu, thời gian, nguồn keyword | Mapping vào VideoResult, bỏ bài ảnh/bình luận, giữ missing là NULL |
| store/douyin/__init__.py | Schema aweme, số liệu dạng chuỗi, cover, timestamp | Mapping riêng Douyin, giữ ID dạng chuỗi và loại chưa rõ thì bỏ qua |
| tools/async_file_writer.py | JSON là mảng record; JSONL là từng object mỗi dòng | Hỗ trợ hai format, UTF-8/BOM; file lỗi cú pháp bị từ chối toàn bộ |

Các đường dẫn trên nằm dưới permalink commit đã ghi. Ví dụ:
- https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/store/xhs/__init__.py
- https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/store/douyin/__init__.py
- https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/tools/async_file_writer.py

Upstream dùng browser context cùng client gọi endpoint website có chữ ký, và có các cơ chế stealth/proxy. Tool hiện giữ yêu cầu master plan: người dùng tự xử lý login/CAPTCHA, không bypass hoặc tự chuyển proxy để vượt chặn. Tham khảo source không chứng minh một API là API chính thức hoặc tài khoản đã được cấp quyền.

## Dùng chức năng nhập

1. Trong MediaCrawler, lấy file **content** `.json` hoặc `.jsonl` đã xuất từ dữ liệu bạn được phép sử dụng. Không chọn file comments/creators/cookies.
2. Mở `http://localhost:3000/imports` → chọn file → **Xem trước dữ liệu**.
3. Kiểm tra video hợp lệ, bản ghi bỏ qua và video đã có trong thư viện.
4. Bấm **Nhập … video vào thư viện**. Video mới ở trạng thái **Mới**. Có thể bấm Lưu; bản đã tồn tại giữ nguyên trạng thái và metadata hiện có.
5. Lịch sử ghi rõ **Nhập MediaCrawler**, có liên kết mở lại lượt nhập. Bản ghi không được coi là vừa xác minh trên nền tảng.

Giới hạn: 1 MB UTF-8 và 200 record/file. JSON phải là mảng; JSONL mỗi dòng một object. Parser không nhận raw response API lồng nhiều tầng. Một record sai metadata không chặn các video hợp lệ; JSON sai cú pháp chặn cả file. Preview chưa ghi DB; commit dùng một transaction.

## Mapping và giới hạn

| Đầu vào | Đầu ra / cách xử lý |
|---|---|
| XHS note_id / type=video | platform_video_id; URL explore tạo từ ID 24 hex |
| Douyin aweme_id / aweme_type=0 | ID dạng chuỗi 15–25 chữ số; URL video tạo từ ID |
| title, desc, nickname | title, caption, author_name; không tái dựng thông tin đã ẩn danh |
| liked_count, collected_count, comment_count, share_count | like_count, favorite_count, comment_count, share_count |
| time / create_time | published_at UTC, nhận epoch giây/millisecond |
| tag_list / source_keyword | hashtags / search_query |
| image_list (ảnh đầu) / cover_url | Chỉ HTTPS CDN nằm trong allowlist xhscdn.com/rednotecdn.com/byteimg.com/douyinpic.com và subdomain |
| Giá trị thiếu / không nhận ra | NULL; không gán 0, không dùng thời gian hiện tại thay ngày đăng |
| 1.2万 / 2.5k | Quy đổi 12000 / 2500 từ số rút gọn trong file, không khẳng định độ chính xác hơn nguồn |
| aweme_type khác 0 hoặc XHS khác video | Bỏ qua và ghi lý do, không đoán là video |
| Cookie, xsec_token, creator_hash/user IDs, URL media download | Không chuyển vào VideoResult/SQLite |

URL XHS được bỏ token; việc mở lại có thể cần đăng nhập hoặc lấy liên kết mới từ nền tảng. Không tự khôi phục token. Cover có thể hết hạn/hotlink bị chặn, UI có placeholder. Không tải video hoặc âm thanh. Thời lượng chưa có nguồn tin cậy nên để NULL, UI không hiển thị 0:00 giả.

Code mới: backend/importers/mediacrawler.py, backend/services/import_service.py, backend/api/imports.py. Thêm hai bảng import_batches và import_origins; không sửa cột hiện có. Lưu SHA-256 nội dung để nhận diện lượt nhập, không lưu file gốc. Video đã tồn tại giữ nguồn đầu tiên; lượt nhập mới vẫn có liên kết job–video riêng.

## Phần còn lại cho Phase 3

- Kiểm tra API chính thức và quyền truy cập thực tế; chọn đường đọc trang nếu phù hợp.
- Browser persistent profile riêng cho XHS, không nhập cookie từ MediaCrawler.
- Nút mở browser đăng nhập; job waiting_for_login/waiting_for_user, Resume/Cancel.
- Parser dựa trên trang quan sát được, không giả selector hay kết quả thật.
- Tìm một keyword tiếng Trung và lưu được tập video thật vào SQLite mới đánh dấu Phase 3 hoàn thành.

Adapter import hỗ trợ sử dụng export đã có; không thay thế acceptance của provider tìm trực tiếp.
