# Phase 3 — Xiaohongshu browser provider

**Trạng thái:** **ĐÃ NGHIỆM THU** (2026-09-05). Đã kết nối Chrome thật với profile riêng, tự động nhận diện phiên đăng nhập người dùng, trích xuất DOM video thực tế và lưu thành công 5 video thật (`is_mock=false`) vào SQLite `data/app.db`.

---

## 1. Kết quả Nghiệm thu Thực tế

### 1.1. Tập video thật đã lưu trong SQLite (`data/app.db`)
Thực hiện tìm kiếm trực tiếp với chế độ `xiaohongshu` thật, query `文具开箱`, limit = 5, Job ID: `3d00695a-dbbf-4cfd-a868-f24ee63ae135`:

| STT | Video ID | Tiêu đề | Tác giả | Thời lượng | Lượt thích | Yêu thích | Bình luận | Trạng thái |
|---|---|---|---|---|---|---|---|---|
| 1 | `1353ba00-c34b-4348-984f-129352b2b267` | ✏️拥有了实体emoji铅笔！好萌一只蓝白波点～ | 汪汪历险记 | 15.034s | 19 | 7,952 | 182 | `saved` (Đã lưu) |
| 2 | `31db7075-eea9-4ab0-9c0e-8533f56cc374` | 😽我宣布最适合吃谷人的痛笔出现了✏✨ | 大晗同学 | 48.567s | 74 | 2,116 | 123 | `new` |
| 3 | `415268d1-f0b4-4374-b30c-8cc55102811d` | 草纸推荐 | 风不息 | 32.234s | 1,286 | 13,500 | 244 | `new` |
| 4 | `7918087d-2a97-48da-99fe-c34911ebbb89` | 睡前读物开箱！有些文创确实🔥的有道理…… | 萌好记 | 294.0s | 1 | 11 | 31 | `new` |
| 5 | `893c7a93-a68b-46be-921c-8f18c9191068` | 才0.5💰一卷吗...🥹 | 浓浓 | 17.585s | 34 | 2,957 | 366 | `new` |

- **Xác minh lưu trữ & tương tác:** Video đầu tiên đã được người dùng đánh dấu `saved`. Dừng server và kiểm tra SQLite: dữ liệu 5 video thật và trạng thái `saved` vẫn nguyên vẹn.
- **Tính toàn vẹn metadata:** Không đoán mò chỉ số; nếu không đọc được trường nào thì để `NULL`. Thời lượng đọc chính xác từ thuộc tính `video.duration` của DOM. Lượt thích / yêu thích / bình luận hỗ trợ các định dạng viết tắt tiếng Trung (`万`, `w`, `k`).

---

## 2. Chẩn đoán Kỹ thuật & Giải pháp Triển khai

### 2.1. Vòng lặp đăng nhập (Login Loop Root Cause)
- **Hiện tượng cũ:** Browser luôn dừng ở trạng thái `waiting_for_login` dù người dùng đã quét mã đăng nhập trước đó trên cửa sổ Chrome.
- **Nguyên nhân kỹ thuật:**
  1. Khi người dùng quét QR / đăng nhập trên Chrome tại Việt Nam / quốc tế, Xiaohongshu lưu phiên đăng nhập và gán cookie xác thực (`id_token`, độ dài 136 bytes) trên domain quốc tế **`rednote.com`**.
  2. Code cũ hardcode điều hướng đến **`www.xiaohongshu.com/explore`**. Trên domain này, người dùng chưa có `id_token`, website hiển thị modal đăng nhập `.login-modal` -> kích hoạt trạng thái `waiting_for_login`.
  3. Xiaohongshu luôn cấp cookie `web_session` ẩn danh cho cả khách vãng lai, do đó không thể chỉ dựa vào `web_session` để coi là đã đăng nhập.
- **Giải pháp:**
  - Cập nhật `BrowserService.detect_base_url()`: Quét danh sách cookie của profile. Nếu phát hiện `id_token` trên `.rednote.com`, tự động đặt `base_url = "https://www.rednote.com"`. Ngược lại dùng `xiaohongshu.com`.
  - Thêm bảng chẩn đoán an toàn (`diagnostics`) trên UI: Hiển thị host, path, query param names, trạng thái xác thực (`Đã đăng nhập ✓`), số thẻ bài viết và video tìm thấy. Tuyệt đối không lộ giá trị cookie hay token.

### 2.2. Khắc phục lỗi 404 bài viết (Missing `xsec_token`)
- **Hiện tượng cũ:** Khi trích xuất link từ trang kết quả tìm kiếm, parser cũ đã strip bỏ `xsec_token`, dẫn đến khi mở chi tiết bài viết nền tảng trả về mã lỗi 404 (`error_code=300031`).
- **Giải pháp:**
  - Cơ chế lưu trữ 2 URL riêng biệt:
    - `canonical_url` & `url`: URL chuẩn hóa (`https://www.xiaohongshu.com/explore/<id>`), bỏ param để deduplication trong SQLite.
    - `share_url`: Lưu URL đầy đủ kèm `xsec_token` trích xuất được từ DOM trang tìm kiếm.
  - Trên giao diện Frontend (`VideoCard`): Nút "Mở bản gốc" ưu tiên dùng `video.share_url || video.url`, cho phép người dùng mở xem trực tiếp trên trình duyệt mà không bị lỗi 404.

### 2.3. Tối ưu điều hướng và DOM Extraction
- Điều hướng tìm kiếm dùng URL chuẩn: `/search_result?keyword=<kw>&source=web_explore_feed&type=51`.
- Tự động tìm và click tab "视频" (Video) nếu có trên giao diện để lọc đúng video.
- Chặn tải tài nguyên video stream/audio bằng route abort để tiết kiệm băng thông; tạm dừng video element ngay khi tải DOM.
- Trích xuất author (`author_name`, `author_id`, `author_url`) từ DOM header chi tiết (`.author-container`, `.author-wrapper`, `a[href*="/user/profile/"]`).
- Trích xuất hashtags từ cả caption (`#...`) và các tag link (`a.tag`, `a[href*="/search_result?keyword="]`).

---

## 3. Kiến trúc An toàn & Bảo vệ Người dùng

1. **Profile riêng biệt:** Dùng thư mục `data/browser/xiaohongshu`, độc lập hoàn toàn với Chrome cá nhân.
2. **Không bot bypass / Không proxy / Không stealth:** Người dùng tự thao tác đăng nhập hoặc vượt CAPTCHA khi nền tảng yêu cầu. Hệ thống dừng lịch sự ở trạng thái `waiting_for_user`, hỗ trợ `Resume` và `Cancel`.
3. **Không tải file video:** Không tải file `.mp4`, không repost, chỉ lưu metadata phục vụ nghiên cứu.
4. **Không lưu trữ nhạy cảm:** Cookie, auth token, `id_token` không bao giờ được ghi vào log, database SQLite hay truyền về giao diện frontend.

---

## 4. Ma trận Kiểm thử

- **Unit & Provider Tests:** 55/55 passed (pytest). Bao gồm các test phân loại URL, kiểm tra dual-domain rednote/xiaohongshu, regex hashtag/engagement, và mock search.
- **Typecheck & Lint:**
  - Backend: `ruff check backend tests` (0 lỗi), `mypy backend` (0 lỗi trên 32 modules).
  - Frontend: `npm run lint` (0 lỗi), `npm run typecheck` (0 lỗi).
- **Production Build:** `next build` hoàn thành thành công, compile 9 static routes.
- **Playwright E2E:** 5/5 passed (Search, Save, Library, MediaCrawler import, Browser controls).
- **Live Smoke Test:** Tìm kiếm và lưu 5 video thật vào SQLite thành công.

