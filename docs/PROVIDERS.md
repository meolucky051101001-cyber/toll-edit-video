# Provider contract

`SearchProvider.search(query, limit, filters) -> list[VideoResult]` là async. `close()` giải phóng tài nguyên kể cả cancel/failure. SearchService nhận provider factory, không chứa tên miền/selector của Douyin hay RED.

MockProvider có 6 chủ đề fixture, variation ổn định theo query, tạo số video theo giới hạn mỗi lần gọi và chèn tối đa 3 bản lặp mỗi provider để thử dedup. Mọi record `is_mock=true`, URL reserved `example.invalid`, thumbnail NULL. MockProvider không phát sinh request ngoài máy. Khi chọn AI, chủ đề được gửi sang Gemini bởi adapter riêng.

Factory hiện chỉ hỗ trợ mock. Nếu tắt mock, ném ProviderUnavailableError. Không tạo browser/official API implementation giả.

Lỗi contract đã khai báo: LoginRequiredError, CaptchaRequiredError, RateLimitedError, SelectorChangedError, NetworkError, ProviderTimeoutError, ProviderUnavailableError.

## Provider thật — công việc ở phase sau

1. Xác minh official API và quyền tài khoản thực tế trước.
2. Nếu không có search API phù hợp, adapter Playwright Async đọc kết quả đang hiển thị.
3. Tách browser/parser/selectors trong thư mục từng platform.
4. Profile của người dùng, headed mặc định, 1 worker/platform, scroll/budget giới hạn.
5. Login/CAPTCHA chuyển waiting state, UI mở browser để người dùng tự xử lý, rồi Resume.
6. Không private API trái phép, không cookie người khác, không bypass anti-bot.
7. Chỉ đánh dấu acceptance sau khi lưu được tập kết quả thật vào SQLite.


## AI provider (Phase 2)

AIProvider.expand(query) trả chuỗi JSON; AIService chịu trách nhiệm validation, retry, cache, throttle, fallback. GeminiAIProvider dùng HTTPS generateContent, key trong x-goog-api-key; không URL query/key logging, không redirect, không trả raw provider error ra API. Payload yêu cầu responseJsonSchema, output tối đa 4096 tokens.

Gemini 3.1 Flash Lite mặc định; allowlist thêm Gemini 3.8 Flash theo tài liệu pricing ngày 2026-09-04. Không tự đổi model. Phải có khóa và xác nhận Free Tier/no Billing; đây là xác nhận người dùng. Model availability/pricing cần được kiểm tra lại khi nâng cấp.

Validation yêu cầu đủ original_query/translated_query/primary_keywords/related_keywords/hashtags/negative_keywords/topics, original giữ nguyên, translation có chữ Trung, giới hạn độ dài/số lượng, không nhận field ngoài schema. Negative chỉ là gợi ý hiển thị.
