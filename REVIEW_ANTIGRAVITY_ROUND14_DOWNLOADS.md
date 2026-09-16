# Review vòng 14 — downloader sau sửa vòng 13
Ngày 2026-09-07. Source C:/Users/admin/Projects/ai-video-research-tool.
Review không sửa source, không gọi AI/crawler, không tải video thật hay ghi database người dùng.

## Tiến độ xác nhận
- Route Douyin /api/hybrid/video_data đã sửa, hỗ trợ base có /api.
- Launcher .py absolute thay Python -c; setup yêu cầu Python 3.12 và kiểm tra HEAD khớp commit.
- bytes_downloaded và progress nullable đã đồng bộ UI.
- Adapter bắt buộc ID/type; nested aweme_detail được đối chiếu.
- DownloadService có shutdown và lifespan gọi trước dispose.
- Có force redownload và giữ latest terminal state khi reload.
- Redirect dùng follow_redirects=False và kiểm tra từng URL trước gửi.
- Full download yêu cầu HTTP 200, thêm disk free check.
- MP4 parser kiểm tra top-level box boundaries, nhưng chưa đủ chứng minh file phát được.

## F1 P1 — DNS check chưa khóa IP của kết nối (sửa chưa đủ vòng 13)
service.py: socket.getaddrinfo kiểm tra DNS rồi HTTPX AsyncHTTPTransport bình thường lại kết nối bằng hostname.
Kết quả lần resolve thứ hai có thể khác lần đã kiểm tra. Không có pinned address/custom network backend. Comment “prevent DNS rebinding” chưa đúng.
Sửa transport connect tới IP đã validate, giữ TLS SNI/hostname verification; hoặc chính sách egress thực thi tại socket tương đương. Test DNS lần 1 public/lần 2 private phải không tạo private connection.
Chưa thực hiện khai thác truy cập private network; finding dựa data flow mã nguồn.

## F2 P2 — File không phát được vẫn được đánh completed
service.py _parse_iso_bmff và _probe_video_file:
- moov chỉ cần chứa chuỗi vide bất kỳ, không kiểm tra track/handler/sample metadata đúng cấu trúc.
- Không tìm thấy ffprobe → True.
- ffprobe timeout hoặc exception → True.
Probe tạo ba box ftyp, moov chứa b'vide', mdat chứa b'garbage': is_valid_mp4=True khi ffprobe không có.
Probe TimeoutError từ ffprobe cũng True.
Test “valid.mp4” hiện tự ghép marker và data giả, không phải video phát được; môi trường có ffprobe có thể cho kết quả khác.

Sửa cài/pin ffprobe hoặc parser đủ chặt; thiếu tool/timeout là verification_failed, không thành công. Kiểm tra stream video và duration hợp lệ; dùng fixture MP4 thật nhỏ cùng file hỏng/truncated. Không yêu cầu audio nếu nguồn không có.

## F3 P2 — Start script vẫn có thể báo thành công dù downloader chưa chạy
scripts/start_downloaders.ps1:
- Thiếu virtualenv chỉ Write-Warning, bỏ qua start và vẫn in “Hoàn tất”.
- Port có listener thì mặc định nhận là đúng downloader, không xác minh endpoint/schema/process.
- XHS health dùng / là redirect ra GitHub theo upstream. Invoke-WebRequest theo redirect nên phụ thuộc Internet, có thể đánh sai trạng thái local.
- Deadline 20 giây dùng chung cho hai service thay vì deadline riêng.

Sửa thiếu dependency fail rõ; kiểm tra /openapi.json với route và schema đúng hoặc dedicated identity health, không truy cập GitHub; port occupied khác app phải báo xung đột. Deadline riêng và test start-stop cả hai sidecar.
Tại thời điểm review không tìm thấy python.exe của hai venv theo đường dẫn scripts yêu cầu; không có listener 5555/5556. Chưa được coi là runtime setup hoàn tất.

## F4 P2 — DNS và kiểm tra file đồng bộ chặn event loop
service.py gọi socket.getaddrinfo ngay trong async processing; is_valid_mp4 gọi subprocess.run(timeout=5) trực tiếp sau stream.
Khi DNS chậm hoặc ffprobe chạy lâu, status/cancel/API khác trên cùng event loop bị trì hoãn. Nếu moov lớn, parser còn đọc toàn bộ moov vào RAM.
Sửa DNS async/cached có chính sách an toàn, ffprobe bằng asyncio subprocess hoặc to_thread; đặt deadline tổng, cancel subprocess khi hủy; giới hạn kích thước metadata đọc. Test status/cancel còn đáp ứng trong khi probe chậm.
Phân biệt socket timeout từng lần đọc với deadline toàn job; thêm timeout tổng để nguồn gửi chậm không chiếm slot vô hạn.

## Hạn chế nghiệm thu
140 backend test xanh không chứng minh tải thật. Không có runtime sidecar hoặc live download được xác nhận trong review này. Không chạy lại E2E/build vòng này; không lấy số E2E từ tài liệu làm kết quả mình vừa chạy.
Social Bulk Downloader vẫn là nguồn tham khảo, không có browser bridge mới đã xác minh; không phải lỗi phạm vi vì kế hoạch ưu tiên local adapters.

## Kiểm thử vòng này
- Pytest 140 passed, 2 warnings, 44.33s.
- Ruff passed.
- Mypy passed 45 source files.
- TypeScript --noEmit --incremental false passed.
- Hai probe MP4 độc lập xác nhận F2.
- Read-only kiểm tra đường dẫn venv/listener: chưa sẵn sàng theo cấu hình mặc định.

## Phase tiếp: D3 — chốt runtime và verification
1. Sửa F1 và F2; tests transport/IP, MP4 thật, missing ffprobe/timeout.
2. Sửa F3; setup/start/health/stop reproducible từ commit pin, không cần internet khi health check.
3. Sửa F4; test cancel/status lúc DNS/probe chậm và deadline tổng.
4. Chạy pytest/Ruff/Mypy/TypeScript/lint, build rồi E2E tuần tự. Fixture HTTP phải theo upstream schema và assert route.
5. Nghiệm thu một video mỗi platform: đúng ID/card, file phát được, audio nếu nguồn có; force retry, reload, cancel, shutdown/restart. Ghi rõ blocked nếu cần tài khoản/CAPTCHA.
6. Chỉ sau đó mở batch download hoặc Phase 5 đa nền tảng. Không đánh dấu download hoàn tất trước live acceptance.

