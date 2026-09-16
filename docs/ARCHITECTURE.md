# Kiến trúc AI Video Research Tool

Ứng dụng Windows native, chỉ bind loopback. Phạm vi hiện tại: Phase 0–3.

```text
Next.js :3000 → /api proxy → FastAPI :8000
                              ↓
                        SearchService
                     ↙       ↓       ↘
              SearchProvider Dedup   Ranking
               ↙          ↘           ↓
        MockProvider   Xiaohongshu   SQLite / SQLAlchemy
                       Provider
                           ↓
                     BrowserService
                    (Chrome profile)
```

- Frontend: Next.js App Router, TypeScript, React, Tailwind, các primitive shadcn/ui.
- Backend: FastAPI, Pydantic, SQLAlchemy; async worker trong cùng process.
- Một job chạy tại một thời điểm; job tiếp theo ở pending. Poll mỗi giây, lưu theo batch.
- Provider trả `VideoResult` chuẩn hóa; service không chứa selector hay platform-specific DOM logic.
- SQLite WAL, foreign keys, unique ID/URL/fingerprint, quan hệ job–video nhiều-nhiều (`search_job_videos`).
- Video giữ trạng thái người dùng khi được tìm lại; lịch sử cũ giữ liên kết kết quả.
- Cancel dùng asyncio cancellation, commit theo batch; restart đánh dấu job dang dở failed.
- Mock luôn gắn `is_mock=true`, URL `example.invalid` và không giả video thật.
- AIService → `GeminiAIProvider` tạo từ khóa thật khi yêu cầu, cache/throttle/JSON validation/fallback. Khóa chỉ nằm backend/.env.
- **Xiaohongshu Browser Provider (Phase 3):**
  - Quản lý qua `BrowserService` kết nối tới Chrome hệ thống với profile riêng `data/browser/xiaohongshu`.
  - Tự động nhận diện dual-domain (`rednote.com` vs `xiaohongshu.com`): Kiểm tra sự hiện diện của cookie xác thực `id_token` để chọn đúng base URL mà người dùng đã đăng nhập.
  - Cơ chế Dual-URL: `canonical_url` (chuẩn hóa không param để deduplication trong SQLite) và `share_url` (giữ `xsec_token` để click mở trực tiếp không bị lỗi 404 code 300031).
  - Trích xuất DOM công khai an toàn: Tác giả (`author_name`, `author_id`, `author_url`), tương tác (`like_count`, `favorite_count`, `comment_count`), `duration_seconds` (từ HTML5 video element), `hashtags`.
  - Giám sát an toàn: Bảng chẩn đoán kỹ thuật chỉ hiển thị thông tin cấu trúc (host, path, trạng thái đăng nhập, số link/video), tuyệt đối không để lộ cookie hay token nhạy cảm ra UI hay log.
- Không Docker production, cloud, download file video, Redis hoặc Celery trong scope.

Backend modules dưới `backend/`; chạy module bằng `python -m uvicorn backend.app.main:app` tại root.
DB mặc định `data/app.db`; đổi DATABASE_URL để chuyển backend DB sau này (cần migration + driver).

Tham khảo triển khai: [Next.js](https://nextjs.org/docs/app/getting-started/installation),
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/),
[shadcn manual](https://ui.shadcn.com/docs/installation/manual).

