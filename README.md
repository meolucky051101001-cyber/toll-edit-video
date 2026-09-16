# AI Video Research Tool — local Windows

**Phase 0–3 đã chạy, kiểm thử và nghiệm thu thành công:** Gemini AI tạo từ khóa tiếng Trung thật; Provider Xiaohongshu qua Chrome riêng (`data/browser/xiaohongshu`) đã kết nối live, tự nhận diện phiên đăng nhập và thu thập 5 video thật vào SQLite `data/app.db`; Thư viện hỗ trợ lưu trữ, phân loại và nhập metadata MediaCrawler. Xem chi tiết tại [PHASE3](docs/PHASE3.md) và [ROADMAP](docs/ROADMAP.md).


## 1. Cài đặt lần đầu

1. Cài **Python 3.12 trở lên** từ https://www.python.org/downloads/windows/ (chọn **Add Python to PATH**).
2. Cài **Node.js 22 LTS trở lên** từ https://nodejs.org/ (bao gồm npm).
3. Mở thư mục `ai-video-research-tool`, mở `scripts`, double-click **setup_windows.bat**.
4. Đợi cài thư viện backend/frontend, Chromium và build giao diện. Có Internet ở bước này.
5. Khi thấy `Setup complete`, đóng cửa sổ cài đặt.

Script ưu tiên `.venv` đang có và tìm Python đủ phiên bản. Script tạo `.venv` riêng, không xóa hoặc ghi đè Python/Node hệ thống. Nếu máy có Python kèm Codex, script có thể dùng nó để tạo `.venv`. Không cần Docker. Nếu muốn chỉ định Python riêng, đặt biến `RESEARCH_PYTHON` trỏ tới `python.exe` trước khi chạy setup.

**Windows Defender:** thư mục Documents/OneDrive có thể được bảo vệ bởi Controlled Folder Access. Nếu báo `WinError 2`, `Access denied` hoặc cài đặt không tạo được file, kiểm tra Windows Security → Virus & threat protection → Ransomware protection → Protection history. Cho phép đúng chương trình bạn tin cậy hoặc tự đặt dự án vào thư mục phát triển ngoài Documents. Không cần tắt Defender. Trên máy hiện tại, bản chạy và mã nguồn mới nhất ở `C:\Users\admin\Projects\ai-video-research-tool`; bản OneDrive giữ nguyên snapshot cũ. Dùng start.bat/stop.bat trong Projects. Không cần đổi cấu hình Defender.

## 2. Chạy tool

Double-click **start.bat** ở thư mục gốc. Script sẽ kiểm tra port, khởi động backend và frontend ẩn, đợi sẵn sàng rồi mở:

- Ứng dụng: http://localhost:3000
- Health UI: http://localhost:3000/health
- Backend: http://127.0.0.1:8000/api/health
- API docs: http://127.0.0.1:8000/docs

Nếu port 3000 hoặc 8000 đang dùng, script báo lỗi và không dừng chương trình khác. Nếu vừa sửa code frontend, chạy lại setup để build trước khi start.

## 3. Dừng tool

Double-click **stop.bat**. Script chỉ dừng PID đã ghi của dự án sau khi kiểm tra thời gian bắt đầu và command line. Không `taskkill /IM python.exe` hoặc kill mọi Node process. Timestamp lưu thêm ticks để tương thích PowerShell 5/7; nếu không xác minh được tiến trình, script giữ hồ sơ và báo lỗi. Dữ liệu đã commit giữ nguyên. Job dang dở sau một lần dừng đột ngột được đánh dấu lỗi khi khởi động lại để bạn chạy lại.

## 4. Cấu hình AI

Gemini đã hỗ trợ dịch chủ đề sang tiếng Trung, tạo từ khóa chính/liên quan, hashtag và chủ đề. Khóa được lưu riêng trong `.env`, không trả về giao diện hay ghi log. Trên máy này khóa đã được cấu hình; người dùng xác nhận dự án Free Tier và chưa bật Billing.

1. Mở **Cài đặt → AI query expansion**. Chọn Google Gemini, model `gemini-3.1-flash-lite`.
2. Nếu thay khóa, nhập vào ô password và xác nhận dự án của khóa đó ở Free Tier, chưa bật Billing. Để trống giữ nguyên khóa cũ. Lưu cấu hình; không cần restart.
3. **Kiểm tra kết nối đã lưu** gửi một chủ đề ngắn (hoặc dùng cache). Nút này kiểm tra cấu hình đã lưu, không dùng giá trị chưa lưu trong form.
4. Ở Tìm kiếm, nhập chủ đề rồi bấm **Tạo từ khóa AI**. Xem bản dịch, chọn/bỏ từ khóa hoặc sửa từng dòng, tối đa 10 truy vấn. Sau đó bấm **Tìm video mẫu**.
5. Có thể bật **Tự tạo từ khóa AI khi tìm** để bỏ qua bước preview. Mặc định tắt để không gọi AI ngoài ý muốn. Truy vấn bạn đã chọn/sửa được ưu tiên và không gọi lại AI.

AI chỉ gửi chủ đề lên Google; không gửi video, cookie hoặc dữ liệu thư viện. JSON được kiểm tra bằng Pydantic. Mỗi lần tối đa 3 lần gọi (lần đầu + 2 retry cho lỗi tạm thời/JSON); lỗi khóa hoặc quota không retry. Cache thành công 10 phút, tối đa 64 chủ đề, dùng chung preview/test/search; không tồn tại qua restart. Tối thiểu 6 giây giữa hai lần gọi, một yêu cầu AI chạy cùng lúc, tối đa 3 yêu cầu đang xử lý/chờ. Khi lỗi, dùng query gốc và hiển thị lý do.

Model cho phép được đối chiếu [bảng giá Gemini](https://ai.google.dev/gemini-api/docs/pricing) ngày 2026-09-04. Free Tier có quota; không có nghĩa là dùng vô hạn. Ứng dụng không bật Billing hoặc tự đổi sang model trả phí. Trạng thái Free Tier là xác nhận của người dùng, không phải kiểm toán tài khoản Google.

Từ khóa và bản dịch do AI gợi ý có thể lệch nghĩa với chủ đề mơ hồ; hãy chỉnh trước khi tìm. Negative keywords hiện chỉ hiển thị gợi ý, chưa áp dụng lọc loại trừ. Semantic ranking bằng AI embeddings chưa được triển khai (hiện dùng lexical/bigram matching).

## 5. Login Douyin & Xiaohongshu

Cả Xiaohongshu và Douyin đều đã hỗ trợ tìm kiếm thật qua Chrome riêng:
- **Xiaohongshu**: Mở **Cài đặt → Trình duyệt & đăng nhập → Mở trình duyệt Xiaohongshu** (profile `data/browser/xiaohongshu`). Hệ thống tự động nhận diện domain đăng nhập (`rednote.com` vs `xiaohongshu.com`) dựa trên cookie xác thực `id_token` và hiển thị chẩn đoán an toàn trên UI.
- **Douyin**: Mở **Cài đặt → Trình duyệt & đăng nhập → Mở trình duyệt Douyin** (profile `data/browser/douyin`). Người dùng tự đăng nhập/xác minh trong cửa sổ đó.

Nếu job cần xác minh CAPTCHA hoặc đăng nhập, tiến trình sẽ tạm dừng ở trạng thái `waiting_for_user` hoặc `waiting_for_login`. Sau khi người dùng hoàn thành xác minh trong trình duyệt, bấm **Tiếp tục lượt tìm** để tiếp tục; có thể bấm **Hủy tìm kiếm** bất kỳ lúc nào.

## 6. Tìm kiếm video thật (Xiaohongshu & Douyin)

Trong form tìm kiếm:
1. Chọn chế độ **Xiaohongshu thật** hoặc **Douyin thật**.
2. Chọn số lượng kết quả (5, 10, 20, 50, 100 video).
3. Nhập từ khóa tiếng Trung (hoặc dùng AI query expansion).
4. Bấm **Tìm kiếm**. Hệ thống sẽ tự động điều hướng kết quả tìm kiếm, trích xuất danh sách video thật, đọc metadata chi tiết (tác giả, tương tác, hashtags, thời lượng, ảnh thumbnail) và lưu trực tiếp vào SQLite `data/app.db`.
5. Nút **Mở bản gốc** trên thẻ video:
   - Với Xiaohongshu: ưu tiên mở `share_url` có kèm token `xsec_token` (tránh lỗi 404); nếu bài chỉ có link canonical không token sẽ mở và hiển thị thông báo hướng dẫn.
   - Với Douyin: mở trực tiếp liên kết video chính thức trên `douyin.com`.
   - Các liên kết ngoài hệ thống tin cậy hoặc giao thức không an toàn sẽ bị chặn.

Giữ `USE_MOCK_PROVIDER=true` làm mặc định an toàn khi gọi API cũ; UI gửi chế độ rõ ràng cho từng job. `XHS_BROWSER_CHANNEL=chrome` và `DOUYIN_BROWSER_CHANNEL=chrome` là mặc định; msedge/chromium là lựa chọn cấu hình nếu browser đó đã cài. Không dùng profile Chrome cá nhân.

## 7. Tìm video mẫu

1. Nhập `unbox đồ cute`.
2. Chọn Douyin, Xiaohongshu hoặc cả hai (đây là nhãn nền tảng cho bộ mẫu).
3. Chọn 20, 50, 100 hoặc 200 kết quả tổng cộng.
4. Bấm **Tìm video mẫu**.
5. Xem tiến độ và kết quả xuất hiện dần. Giới hạn là số tối đa; nhiều từ khóa có thể trả kết quả trùng nên ít hơn số đã chọn. Có thể **Hủy tìm kiếm**.
6. Lọc nền tảng, điểm phù hợp, lượt thích, ngày đăng, trạng thái; đổi thứ tự xếp hạng.

Dữ liệu mẫu có URL `example.invalid`, không phải video thật. Thumbnail là thẻ placeholder có nhãn MOCK PREVIEW. Không tải video. Các số tương tác/ngày đăng là fixture mẫu; dữ liệu thực thiếu metadata sau này phải để NULL.

Điểm phù hợp hiện dùng token/keyword overlap (bao gồm bigram tiếng Trung), không phải AI hay embeddings. Điểm chất lượng dùng log-normalized engagement và freshness. Điểm cuối = 80% phù hợp + 20% chất lượng.

## 8. Lưu và xem lại

- Bấm **Lưu** trên card; mở **Thư viện → Đã lưu**.
- Bấm lại **Đã lưu** để trả trạng thái Mới.
- **Bỏ qua**, **Yêu thích**, **Đã dùng** được lưu trong SQLite.
- Bấm tên/card để xem caption, query, điểm số và copy URL mẫu.
- **Lịch sử** mở lại kết quả; nút chạy lại điền chủ đề vào form để bắt đầu lượt mới.
- Chạy lại query vẫn chạy provider; DB loại trùng, giữ trạng thái người dùng.
- Bộ sưu tập, export và xóa lịch sử chưa được triển khai. Cài đặt AI có thể chỉnh trên UI.

## 9. Troubleshooting

| Hiện tượng | Cách xử lý |
|---|---|
| Backend offline | Chạy start.bat, xem logs/backend-error.log. |
| Port đang dùng | Nếu app này đang chạy, mở localhost:3000; nếu cần khởi động lại, chạy stop.bat trước. |
| Không thấy video đã lưu | Mở đúng tab Đã lưu, bỏ bộ lọc nền tảng/từ khóa/ngày. Kiểm tra đúng bản dự án/data. |
| Tìm kiếm thất bại vì real provider | Đặt USE_MOCK_PROVIDER=true trong .env, stop rồi start. |
| WinError 2 / Access denied khi cài | Kiểm tra Protection history của Defender như hướng dẫn cài đặt. |
| npm/pip không tải được | Kiểm tra mạng rồi chạy lại setup. Không xóa data. |
| Dừng máy giữa job | Mở Lịch sử, chạy lại job. Những video đã commit vẫn còn. |
| UI không cập nhật sau sửa code | Stop, chạy setup để build lại, rồi start. |

Log nghiệp vụ: `logs/app.log` (xoay vòng), log server ở `logs/backend*.log`, `logs/frontend*.log`. Không ghi API key/password/cookies vào log. Query tìm kiếm được ghi để debug, vì vậy không chia sẻ log nếu chủ đề nhạy cảm.

## Dữ liệu và sao lưu

DB mặc định: `data/app.db`. Dừng app rồi sao lưu toàn bộ `data/` (bao gồm WAL nếu còn). `.env`, database, log, profile, dependencies đều bị gitignore. Không chia sẻ `.env` hoặc browser profile. Backend chỉ bind `127.0.0.1`; frontend proxy `/api` đến backend, không public cloud.

## Dành cho người phát triển

Chạy các lệnh từ thư mục `ai-video-research-tool` trong PowerShell:

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests
.\.venv\Scripts\python.exe -m mypy backend
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
```

Sau khi start app (MockProvider):

```powershell
npm.cmd --prefix frontend run test:e2e
```

E2E tạo dữ liệu mẫu trong DB ứng dụng, không xóa thư viện của bạn. Test backend dùng DB tạm riêng.

Chạy dev (hai cửa sổ PowerShell riêng, bắt đầu ở root dự án):

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

```powershell
npm.cmd --prefix frontend run dev
```

Tài liệu: [Architecture](docs/ARCHITECTURE.md), [Database](docs/DATABASE.md), [Providers](docs/PROVIDERS.md), [Pipeline](docs/SEARCH_PIPELINE.md), [Roadmap](docs/ROADMAP.md).

## Kết quả nghiệm thu và bộ tiêu chuẩn kiểm thử

Hệ thống được bảo đảm chất lượng liên tục qua toàn bộ các release gate tự động (chạy sạch 100% trước khi bàn giao):
- **Pytest:** Kiểm tra toàn bộ logic backend, schema contracts, URL policies, dedup, ranking, AI expansion và provider parser.
- **Ruff:** Linter và code style backend không có vi phạm (`ruff check backend tests`).
- **Mypy:** Kiểm tra kiểu dữ liệu tĩnh nghiêm ngặt trên toàn bộ 36 file mã nguồn backend (`mypy backend --no-incremental`).
- **TypeScript:** Kiểm tra kiểu dữ liệu tĩnh frontend (`tsc --noEmit --incremental false`).
- **ESLint:** Linter frontend đạt chuẩn Next.js/React (`eslint .`).
- **Playwright E2E:** Kiểm tra end-to-end trình duyệt tự động cho các luồng nhập MediaCrawler, tìm kiếm mẫu, tìm kiếm Xiaohongshu & Douyin thật, pause/resume login, bảo toàn token, và chuẩn hóa tham số giao diện.
- **Next.js Production Build:** Build sạch sẽ toàn bộ static routes và dynamic API proxy.

Xem thêm tài liệu kỹ thuật chi tiết tại [VALIDATION](docs/VALIDATION.md) và [PHASE3](docs/PHASE3.md).

## Nhập metadata từ MediaCrawler

Mở **Nhập dữ liệu** ở sidebar hoặc http://localhost:3000/imports. Chọn file content JSON/JSONL đã xuất (tối đa 1 MB, 200 record), xem trước rồi bấm nhập. Hỗ trợ Xiaohongshu type=video và Douyin aweme_type=0. Bài ảnh, comments và bản ghi không hợp lệ được liệt kê để bỏ qua. Video trùng giữ metadata và trạng thái đang có; video mới ở trạng thái Mới.

Dữ liệu nhập có nhãn **Nhập MediaCrawler**, chưa xác minh lại trên nền tảng. Không gọi AI/crawler hoặc tải video trong bước nhập. Cách đối chiếu nguồn, giấy phép và giới hạn mapping: [MEDIACRAWLER.md](docs/MEDIACRAWLER.md). Cả Xiaohongshu và Douyin đều đã hỗ trợ tìm trực tiếp qua trình duyệt với profile riêng.
