# Hướng Dẫn Sử Dụng Bot Tải Video Douyin & Xiaohongshu (Độc Lập)

## 1. Giới thiệu
Bot Telegram chuyên trách việc **bóc tách và tải video nguyên bản không logo / watermark** từ:
- **Douyin (抖音)**: Hỗ trợ link rút gọn `v.douyin.com`, link web `douyin.com/video/...`, link có `modal_id`, văn bản sao chép kèm text.
- **Tiểu Hồng Thư (Xiaohongshu / RedNote)**: Hỗ trợ link `xhslink.com`, link `xiaohongshu.com/explore/...`. Hỗ trợ cả Album ảnh (gửi ảnh gốc nếu bài không phải video).
- **TikTok**: Hỗ trợ link `vt.tiktok.com`, `tiktok.com/@.../video/...`.

> [!IMPORTANT]
> **Hoàn toàn độc lập với Tool V1**:
> - File chạy riêng: `telebot_video_downloader.py`.
> - Không sửa đổi hay ghi đè bất kỳ file nào của Tool V1 (`telegram_bot.py`, `main.py`, `.env` vẫn giữ nguyên bản cho luồng dubbing / lồng tiếng).
> - Token Bot mới: `<REDACTED_TELEGRAM_BOT_TOKEN>` (Username: `@douxiao_bot`).

---

## 2. Cách khởi động Bot

### Cách 1: Chạy bằng file `.bat` (Khuyên dùng)
- Nhấp đúp chuột vào file:
  `C:\tool v1\start_telebot_downloader.bat`
- Cửa sổ console sẽ hiện thông báo Bot đang chạy.

### Cách 2: Chạy bằng dòng lệnh (Terminal)
Mở PowerShell hoặc CMD trong thư mục `C:\tool v1`:
```bash
.\backend\venv\Scripts\python.exe telebot_video_downloader.py
```

### Cách tắt Bot:
- Nhấn `Ctrl + C` trên cửa sổ đang chạy, hoặc:
- Nhấp đúp vào file `C:\tool v1\stop_telebot_downloader.bat`.

---

## 3. Cách sử dụng trên Telegram
1. Mở Telegram và tìm kiếm bot: **`@douxiao_bot`** (hoặc nhấn nút Start nếu đã mở chat).
2. Gửi lệnh `/start` để xem hướng dẫn.
3. **Dán bất kỳ link Douyin hoặc Xiaohongshu** vào khung chat.
4. Bot sẽ tự động:
   - Nhận diện link và bóc tách luồng video sạch không logo.
   - Tải video với tốc độ cao (đa luồng HTTP Range 6 workers).
   - **Tự động lưu file video vào thư mục:** `D:\vid chưa edit`
   - Gửi file video `.mp4` trực tiếp vào khung chat Telegram cho bạn.
   - Tự động đặt tên file chống trùng lặp.

---

## 4. Các tính năng nâng cao đã tích hợp
1. **Đa tầng giải mã Douyin**:
   - Tầng 1: Động cơ Montague / Viesnap (~1.5s - 2s).
   - Tầng 2: Direct Web API của Tool V1.
   - Tầng 3: SO9 Downloader dự phòng.
   - Tầng 4: Mobile Feed trực tiếp từ máy chủ ByteDance (100% không cần cookie, không cần sign).
2. **Loại bỏ watermark Xiaohongshu 100%**:
   - Tự động quét và chọn luồng Stream 258 (Desktop H.264) hoặc originVideoKey (gốc tác giả tải lên).
   - Tự động chặn luồng Stream 259 (WeChat Mini App) bị dính logo watermark.
3. **Hỗ trợ Album ảnh Tiểu Hồng Thư**:
   - Nếu link là bài viết chia sẻ ảnh (không phải video), Bot sẽ tự động gửi bộ ảnh chất lượng cao về cho bạn.
