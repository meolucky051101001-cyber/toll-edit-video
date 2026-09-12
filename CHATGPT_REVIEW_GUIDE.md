# TÀI LIỆU HƯỚNG DẪN REVIEW TOÀN DIỆN (DÀNH CHO CHATGPT / SENIOR AI CODE AUDITOR)

**Dự án:** AutoDub Video Bot (Tool V1.0 Final)  
**Mục đích:** Báo cáo kỹ thuật và danh mục các thay đổi trong toàn bộ đợt nâng cấp toàn diện **MASTER FIX PLAN (Phase 0 đến Phase 8)** để ChatGPT hoặc chuyên gia kiểm tra, thẩm định chất lượng code (Code Review & Security Audit).

---

## 1. TỔNG QUAN HỆ THỐNG & BỐI CẢNH (ARCHITECTURE & CONTEXT)

AutoDub Video Bot V1 là hệ thống tự động hoá biên dịch và lồng tiếng video ngắn (Douyin, Xiaohongshu, TikTok, YouTube Shorts) sang tiếng Việt chạy trên máy trạm Windows với GPU NVIDIA (RTX 4050 6GB VRAM).

### Các thành phần chính trong Pipeline:
1. **Downloader (social_downloader.py):** Tải video sạch không logo từ các mạng xã hội qua HTTP stream hoặc yt-dlp.
2. **Audio Separation (demucs):** Tách vocal của nhân vật và nhạc nền gốc.
3. **ASR (aster-whisper):** Nhận dạng tiếng Trung, tách mốc thời gian phụ đề theo câu/từ (word timestamps).
4. **OCR & Subtitle Tracking (ppocr/easyocr):** Nhận diện vị trí card phụ đề gốc tiếng Trung trên từng frame để xóa/làm mờ (blur band) và gắn phụ đề mới.
5. **Translation (ackend/ai/translation.py):** Dịch phụ đề SRT sang tiếng Việt qua Gemini API (Gemini 3.8 Flash / 1.5 Flash), FPT AI, hoặc Deep-Translator.
6. **TTS & Voice Clone (ackend/ai/voice_cloning.py):** Lồng tiếng tiếng Việt qua Edge-TTS, CapCut TTS hoặc RVC Voice Cloning.
7. **Audio Mixer & Video Render (ss_utils.py, ackend/main.py):** Tạo file ASS căn chỉnh tọa độ, hòa âm vocal + background music qua pydub, render video hoàn chỉnh qua FFmpeg.
8. **Điều khiển & Giao diện:**
   - **Telegram Bot (	elegram_bot.py):** Nhận link, xử lý hàng đợi, gửi video kết quả.
   - **Web Dashboard & A2UI Studio (main.py - FastAPI port 8088):** Giám sát trạng thái pipeline, xem timeline, chỉnh sửa phụ đề và cấu hình lồng tiếng.
   - **Tool Controller (	ool_control.py - HTTP port 8090):** Quản lý vòng đời tiến trình (Start/Stop/Restart bot và dashboard).

---

## 2. NHỮNG VẤN ĐỀ TRƯỚC ĐÂY & GIẢI PHÁP ĐÃ TRIỂN KHAI (PHASE 0 -> PHASE 8)

| Phase | Vấn đề ban đầu (Critical / High Bugs) | Giải pháp & Thay đổi kiến trúc đã thực hiện |
| :--- | :--- | :--- |
| **Phase 0** | Codebase không có version control, không có baseline test, nguy cơ vỡ toàn bộ khi sửa. | Khởi tạo Git repo chuẩn, thêm .gitignore loại bỏ rác/model weights, tạo baseline test suite 183 tests. |
| **Phase 1** | **OCR Fail-Open:** Khi OCR lỗi hoặc timeout, pipeline vẫn render đè phụ đề tiếng Việt lên phụ đề tiếng Trung bị vỡ.<br>**TTS Silent Fallback:** TTS lỗi trả về audio rỗng (0 bytes), video xuất ra bị câm tiếng. | **Chuyển sang cơ chế Fail-Closed:**<br>- OCR lỗi sẽ dừng pipeline hoặc fallback rõ ràng, tuyệt đối không render video lỗi.<br>- TTS kiểm tra chặt chẽ độ dài audio và định dạng wav; nếu TTS thất bại thì đánh dấu Job FAILED, cấm xuất file câm. |
| **Phase 2** | **Tràn VRAM / CUDA OOM:** Chạy 3-5 video liên tiếp bị tràn 6GB VRAM RTX 4050 do Whisper, Demucs và PaddleOCR giữ bộ nhớ GPU trong cùng 1 tiến trình Python. | - Đưa OCR và Whisper vào mô hình tiến trình con cô lập (Subprocess Worker / 
um_workers=1).<br>- Thu hồi tiến trình sau mỗi batch, gọi tường minh 	orch.cuda.empty_cache() và gc.collect(). |
| **Phase 3** | **Dịch lỗi treo pipeline:** Gemini API gặp rate limit 429 hoặc timeout làm bot đơ vĩnh viễn.<br>Không có cache làm tốn token API khi render lại.<br>Dịch lỗi dẫn đến việc đọc tiếng Trung nguyên bản vào TTS. | - Xây dựng hệ thống Cache dịch thuật cục bộ (1_translation_cache.py) theo hash nội dung.<br>- Thêm cơ chế Fallback đa tầng: Gemini 3.8 Flash -> Gemini 1.5 Flash -> Deep-Translator -> FPT.<br>- Timeout giới hạn (25s) và Exponential Backoff.<br>- Bộ lọc phát hiện tiếng Trung trước khi đẩy sang TTS. |
| **Phase 4** | **Downloader treo vô hạn:** Tải video từ XHS/Douyin mất mạng làm luồng download đứng im mãi mãi.<br>Partial download (.part) bị đọc nhầm thành video hoàn tất.<br>**Race Condition History:** Web Dashboard và Telegram bot ghi đồng thời vào file history làm hỏng JSON. | - Bổ sung Socket/Stream timeout và yt-dlp timeout nghiêm ngặt.<br>- Dọn dẹp file .part dở dang khi bị hủy hoặc timeout.<br>- Triển khai File Lock đa tiến trình (.render_history.lock) và cơ chế Atomic Write (ghi temp rồi replace) cho file lịch sử render. |
| **Phase 5** | **Bảo mật API & Khóa luồng:**<br>- API không có token, máy khác trong mạng LAN có thể gửi request.<br>- Lỗ hổng Path Traversal (ideo_path) có thể đọc/ghi file hệ thống.<br>- FastAPI Event Loop bị nghẽn (freeze UI) khi chạy hàm FFmpeg/Whisper đồng bộ.<br>- 	ool_control.py kill nhầm process khác đang mở port 8090. | - Thêm FastAPI Middleware kiểm tra Host (127.0.0.1/localhost) và bắt buộc header X-Local-Control-Token ngẫu nhiên.<br>- Hàm _validate_input_path chống Path Traversal (../) và giới hạn thư mục hợp lệ.<br>- Bọc toàn bộ các hàm media nặng bằng wait asyncio.to_thread(), giúp Dashboard luôn mượt mà.<br>- Sửa logic kill process của Controller: chỉ kill đúng tiến trình 	ool_control.py/	elegram_bot.py. |
| **Phase 6** | **Khó triển khai máy mới:** File .env chứa API key thật trong repo. equirements.txt thiếu nhiều thư viện runtime (psutil, 
umpy, Pillow). Thư mục rontend/ (Electron cũ) bị hỏng code nguồn gây rác. | - Tạo ackend/.env.example, đưa .env vào .gitignore.<br>- Bổ sung đầy đủ 100% direct dependencies vào equirements.txt.<br>- Viết script tự động cài đặt setup_v1.ps1 (1-click install).<br>- Xóa sạch thư mục Electron cũ, tinh gọn mã nguồn. |
| **Phase 7** | Cần đảm bảo không có regression sau hàng loạt thay đổi. | - Kiểm tra tĩnh: compileall đạt 100% không lỗi cú pháp.<br>- Chạy toàn bộ 183 bài tests: **183/183 PASSED** (0 failures). |
| **Phase 8** | Dọn dẹp mã nguồn thừa, file backup, hoàn tất đóng bản V1 Final. | - Xóa các file *.before-*, *.bak, code tạm thời.<br>- Viết lại toàn bộ README.md mới chuẩn mực cho bản V1.0 Final.<br>- Đóng gói bản phát hành sạch. |

---

## 3. DANH SÁCH TỆP THAY ĐỔI CHÍNH (KEY FILES MODIFIED)

1. ackend/main.py:
   - Bổ sung security_middleware (Local Host check + X-Local-Control-Token).
   - Bổ sung hàm _validate_input_path(video_path) chống Path Traversal.
   - Bọc các tác vụ nặng (extract_audio_from_video, extract_subtitles_whisper, 	ranslate_subtitles, process_video, mix_audio_pydub) qua syncio.to_thread.
2. ackend/tool_control.py:
   - Thêm token xác thực cho các thao tác tiến trình.
   - Sửa hàm tìm tiến trình: kiểm tra tên python và dòng lệnh chứa 	ool_control.py/	elegram_bot.py trước khi kill, không kill bừa theo port.
   - Cơ chế dọn bot cũ trước khi start bot mới tránh duplicate.
3. ackend/social_downloader.py:
   - Bổ sung timeout cho kết nối HTTP stream và tiến trình yt-dlp.
   - Cơ chế tự động dọn dẹp file partial rác (.part, .ytdl) khi xảy ra lỗi.
4. ackend/render_history.py:
   - Triển khai cross-process file lock (.render_history.lock).
   - Ghi file an toàn qua file tạm và atomic replace, tự phục hồi nếu file JSON bị hỏng.
5. ackend/ai/translation.py:
   - Đổi mới kiến trúc dịch thuật với Cache nội bộ, retry timeout và fallback nhiều model.
6. ackend/ai/voice_cloning.py:
   - Kiểm tra chặt chẽ đầu ra âm thanh, loại bỏ silent fallback gây câm tiếng.
7. ackend/templates/dashboard.html & 2ui_studio.html:
   - Tự động nhúng LOCAL_TOKEN vào window.fetch gửi header X-Local-Control-Token.
8. equirements.txt & setup_v1.ps1:
   - Chuẩn hoá toàn bộ danh mục package và script tự động setup venv.
9. 	ests/:
   - Bộ test suite 183 bài kiểm thử độ ổn định (Whisper, OCR, Translation, History Lock, API).

---

## 4. GỢI Ý CÁC MỤC CHATGPT CẦN KIỂM TRA (CHECKLIST FOR CHATGPT AUDIT)

Kính nhờ ChatGPT / AI Reviewer kiểm tra sâu các khía cạnh sau:

1. **Bảo mật (Security Audit):**
   - Kiểm tra security_middleware trong ackend/main.py: Cơ chế kiểm tra host 127.0.0.1 và X-Local-Control-Token đã đủ chặt chẽ để chống CSRF / DNS Rebinding / LAN Attack chưa?
   - Kiểm tra _validate_input_path: Đã xử lý triệt để các dạng bypass path traversal trên Windows (như UNC paths, \\?\, drive traversal) chưa?
2. **Đồng thời & Khóa tệp (Concurrency & Thread Safety):**
   - Kiểm tra ender_history.py: Cơ chế lock .render_history.lock và atomic write bằng os.replace trên Windows có gặp rủi ro file locking của OS không?
   - Kiểm tra việc dùng syncio.to_thread: Có biến dữ liệu toàn cục (global state) nào bị race condition khi nhiều luồng FastAPI cùng truy cập không?
3. **Quản lý Tài nguyên & Tiến trình (Resource & Process Management):**
   - Kiểm tra 	ool_control.py: Cơ chế kill tiến trình qua psutil đã an toàn tuyệt đối và dọn sạch cây tiến trình con (zombie processes) chưa?
   - Kiểm tra quản lý GPU: Việc gọi 	orch.cuda.empty_cache() và giải phóng session OCR/ASR có khả năng rò rỉ bộ nhớ dài hạn nào không?
4. **Xử lý Ngoại lệ (Exception Handling & Resilience):**
   - Cơ chế Fail-Closed của OCR/TTS: Đã bao quát hết các trường hợp timeout, corrupt audio, corrupt subtitle format chưa?
   - Kiểm tra chất lượng các test cases trong 	ests/.

---

## 5. KẾT QUẢ KIỂM THỬ (TEST RESULTS)

`	ext
============================= test session starts =============================
platform win32 -- Python 3.10.11, pytest-9.1.1
collected 183 items

... (toàn bộ 183 test cases trải rộng qua các module AI, OCR, Downloader, API) ...

============= 183 passed, 3 warnings, 7 subtests passed in 14.56s =============
`
- **Tỷ lệ Pass:** 100% (183/183)
- **Lỗi Critical/High còn lại:** 0
- **Regression:** 0
