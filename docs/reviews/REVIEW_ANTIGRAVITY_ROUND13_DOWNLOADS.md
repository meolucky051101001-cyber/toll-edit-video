# Review vòng 13 — downloader và xác minh vòng 12
Ngày 2026-09-07. Codebase C:/Users/admin/Projects/ai-video-research-tool.

## Kết luận
Đã có triển khai thực về cấu trúc: hai adapter, DownloadService, DownloadJob, REST API, file tạm, giới hạn dung lượng, semaphore, cancel và UI dưới card/modal.
Chưa đạt nghiệm thu tải thực. Hai virtualenv upstream không tìm thấy tại đường dẫn script yêu cầu khi kiểm tra; không có bằng chứng tải video thật end-to-end vòng này.
Social Bulk Downloader chỉ là tham khảo, chưa thấy browser bridge tích hợp. Điều này phù hợp kế hoạch trước: không yêu cầu sao chép extension.

## Vòng 12
Probe xác nhận conflict ID B/canonical A bị validate_result_identity từ chối; short link token giả không còn đè token A đã xác minh. Có policy chung/schema/persistence/dedup.
Lưu ý policy vẫn cho unresolved short vào preferred share_url khi chưa có existing link verified, trước fallback record canonical. Đây là phần còn yếu; ưu tiên canonical đã biết hoặc lưu short trong provenance cho đến resolve.
Resolver short link cũng follow_redirects tự động: cần áp dụng cách kiểm tra redirect trước request như mục dưới.

## Findings ưu tiên
### F1 P1 — Douyin adapter sai route theo upstream thực
backend/services/download/douyin_adapter.py: endpoint dùng /hybrid/video_data.
Config default base http://127.0.0.1:5555; upstream app/main.py include_router(prefix=/api), router.py prefix=/hybrid, hybrid_parsing.py route=/video_data.
Route đúng với cấu hình mặc định: /api/hybrid/video_data. Route hiện tại 404, chặn tải Douyin.
Sửa endpoint; test bằng contract route đúng của upstream, assert method/path/query; không monkeypatch get trả thành công cho mọi URL.

### F2 P1 — Redirect được kiểm tra sau khi đã truy cập; DNS hostname không được kiểm tra IP
backend/services/download/service.py:353-357.
follow_redirects=True tự gửi tới đích redirect, rồi mới kiểm tra response.url cuối. Comment nói kiểm tra mỗi redirect nhưng thực tế không làm vậy.
is_safe_media_url chỉ chặn IP literal, cho hostname phân giải về private IP qua.
Sửa follow_redirects=False, kiểm tra từng Location trước request, giới hạn hop, validate host + resolved addresses và kết nối tới IP đã kiểm tra để tránh DNS rebinding. Chỉ chuyển header cần thiết; không mang credentials sang host khác.
Test transport ghi nhận public→private redirect không hề được request; hostname→private bị chặn. Áp dụng cho short-link resolver mới.
Không thực hiện truy cập mạng nội bộ trong review; kết luận dựa luồng source và probe validator.

### F3 P1 — Lệnh chạy XHS không quote đoạn Python
scripts/start_downloaders.ps1: $xhsCode truyền trực tiếp vào Start-Process -ArgumentList @('-c',$xhsCode). Trên Windows arguments được nối, Python có thể nhận chỉ 'import' làm code và thoát SyntaxError.
Dùng launcher .py riêng, truyền đường dẫn tuyệt đối được quote đúng. Script phải kiểm tra process chưa thoát và health endpoint trước báo thành công; ngủ 2 giây không đủ.
Ngoài ra setup chấp nhận Python 3.10 trong khi upstream XHS yêu cầu >=3.12. Setup hiện chỉ cảnh báo khi thiếu repo nhưng vẫn báo thành công; phải fail hoặc tự fetch commit được pin.

### F4 P2 — Contract progress frontend/backend khác nhau
backend/schemas/downloads.py trả bytes_downloaded và progress_percent nullable.
frontend/types/index.ts và controls dùng downloaded_bytes và progress_percent number.
Frontend không có mapper, nên số MB thực hiển thị 0 khi đang tải. E2E fixtures cũng dùng downloaded_bytes nên che lỗi.
Sửa type và UI theo schema backend; xử lý total/progress null bằng indeterminate, không giả phần trăm.
Sinh fixture từ JSON API thật, test bytes >0 khi total unknown.

### F5 P2 — Adapter không bắt buộc ID/type trả về
Cả hai adapter chỉ reject mismatch nếu resp_id có giá trị; thiếu ID thì tự dùng expected_id và vẫn trả resolved media. Thiếu type cũng được chấp nhận.
Không chứng minh file thuộc card; fallback aweme_detail Douyin không xác minh aweme_id của nested detail.
Sửa schema response nghiêm ngặt theo từng adapter, bắt buộc ID/type/platform và đối chiếu expected; missing data phải fail với lỗi schema.
Test missing ID, missing type, nested mismatch, non-dict video_data. Không coi expected ID là bằng chứng upstream.

### F6 P2 — Script stop không xác minh được tiến trình đã start
start_downloaders dùng python -m uvicorn app.main:app với đường dẫn module tương đối; process helper Stop-RecordedProcess đòi CommandLine chứa ProjectRoot.
Đường dẫn python.exe không được đảm bảo hiện diện trong CommandLine khi Windows khởi chạy bằng tên executable; cần kiểm chứng trên tiến trình thực và dùng launcher absolute/record executable cùng startTicks để đối chiếu.
Đây là rủi ro vận hành từ cách start/stop, cần test chạy–dừng hai sidecar; không hạ bỏ kiểm tra an toàn.
Pin commit mới chỉ là string trong adapter; setup chưa checkout/verify HEAD. Phải xác nhận git rev-parse HEAD khớp trước launch/version reporting.

### F7 P2 — Shutdown không dừng download tasks
backend/app/main.py lifespan chỉ shutdown search/browser và engine.dispose. DownloadService không có shutdown để cancel/gather active_tasks trước DB dispose.
Thêm shutdown có chờ task, dọn part, persist trạng thái gián đoạn, rồi mới dispose DB. Test stop giữa stream và restart.

### F8 P2 — File “MP4 hợp lệ” chỉ được kiểm tra vài byte
is_valid_mp4 tìm ftyp/moov/mdat trong 32 byte đầu; file chỉ có header rồi zeros vẫn được test coi valid.
Đây chỉ là sniff định dạng, không chứng minh file hoàn chỉnh/phát được. 206 cũng được chấp nhận dù request không Range, không kiểm tra Content-Range.
Chỉ chấp nhận 200 cho full download; nếu resume thì kiểm tra range/length/validator đầy đủ. Kiểm tra container có video stream, duration bằng ffprobe hoặc parser tương đương trước báo completed; không bắt buộc audio nếu nguồn không có.
Thêm test truncated MP4, incomplete 206, stream hợp lệ có/không audio.

### F9 P3 — Reload và Tải lại không đúng trạng thái
get_active_job_for_video chỉ trả active/completed, bỏ failed/interrupted/cancelled nên reload mất lỗi và nút retry cụ thể.
Nút “Tải lại từ đầu” gọi request_download nhưng backend tái sử dụng completed file, không thực sự tải lại.
Trả latest terminal job khi không active/completed; thêm force retry rõ ràng hoặc đổi nhãn UI đúng hành vi.

## Các phần còn thiếu theo kế hoạch
- Chưa thấy kiểm tra free disk trước tải.
- Retry transport chỉ giúp connect, chưa có re-resolve URL khi media hết hạn.
- Chưa có cấu hình phiên đăng nhập upstream rõ ràng; Chrome research đang login không tự đồng nghĩa sidecar đã có cookie.
- Không nên xuất res.text hoặc exception chứa URL/token vào error_message; sanitize lỗi adapter/stream.
- Semaphore global trước platform có thể để các job cùng platform chiếm slot chờ và làm platform còn lại phải đợi.
- Không có chứng minh resume download theo Social Bulk Downloader; hiện chủ yếu retry job mới, cần mô tả đúng.

## Kiểm thử review
Pytest 130 passed, 2 warnings, 121.18s; Ruff passed; TypeScript --noEmit --incremental false passed.
Không chạy lại full E2E hoặc build trong review này. Đã đọc E2E fixtures downloader và thấy field mismatch.
Đối chiếu source upstream local: Douyin route /api/hybrid/video_data, XHS /xhs/detail.
Không cài dependencies, không start/stop server, không tải video thật, không sửa source hoặc DB người dùng.

## Plan bàn giao Antigravity
D0: sửa F1/F3, bootstrap reproducible pin commit, Python 3.12+, launcher absolute, health identity/readiness, start-stop test. Cài sidecar thật vào venv riêng.
D1: sửa F2/F5/F7/F8, strict response schema/identity, media request an toàn, lifecycle, file validation, sanitize errors.
D2: sửa F4/F9, progress contract thống nhất, trạng thái bền qua reload, retry đúng nghĩa.
D3: test xuyên frontend→API→DB→fake sidecar HTTP→stream→file, dùng response đúng upstream, không stub mọi URL cùng thành công.
D4: nghiệm thu một video XHS và một Douyin thật: file phát được, đúng card, có âm thanh nếu nguồn có; cancel/retry/restart, login required hiển thị rõ. Lưu biên bản đã lọc token/cookie.
Chỉ đánh dấu hoàn tất downloader sau D4. Chưa ưu tiên batch download/extension bridge/Phase 5 song song.

