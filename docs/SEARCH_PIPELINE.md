# Search pipeline Phase 2

1. POST /api/search validate query/platform/limit 1–200, optional use_ai và selected_queries (1–10); tạo SearchJob + SearchPlan, trả HTTP 202.
2. Một job chạy, tối đa 10 job active/queued. Query thủ công được ưu tiên; không gọi AI nếu đã chọn query.
3. Nếu use_ai=true, trạng thái expanding_query: AIService dùng cache hoặc Gemini, validate JSON/original_query, tối đa 2 retry. Lỗi/quota trả query gốc kèm warning. Không giữ transaction DB trong lúc chờ API.
4. Lưu bộ query (tối đa 10), source, warning và expansion vào search_plans. Job cũ thiếu plan dùng query gốc.
5. Chia result budget tổng cho các platform; chia phần còn lại cho từng query. SearchQuery ghi mỗi truy vấn thực sự đã gọi. SearchProvider luôn close trong finally.
6. Mỗi 8 record: normalize → dedup ID/URL/fingerprint → rank → upsert metadata → link job/video → commit; không gắn quá budget tổng hoặc platform. Kết quả trùng có thể làm số video thấp hơn giới hạn.
7. Poll mỗi giây; completed/failed/cancelled. Cancel khi AI đang chạy giải phóng limiter; shutdown đánh dấu các job bị ngắt.

`found_count` gồm duplicate; `processed_count` là unique trong job; `duplicate_count` đếm lần đã tồn tại trong DB, gồm các job trước. Không dùng phép trừ các số này để suy ra số kết quả.

Ranking vẫn lexical trên query gốc: accent-insensitive tokens + Chinese bigrams, không embeddings/LLM ranking. Quality tách engagement khỏi relevance, dữ liệu thiếu vẫn NULL.

POST /api/search/expand không tạo job; dùng cùng AIService như search và POST /api/settings/ai/test. 1 request AI chạy, tối đa 3 active/queued, timeout tổng 85s bao gồm chờ, timeout HTTP 20s, min interval 6s, cache 600s/64 query. HTTP 429: không retry, cooldown ít nhất 60s; không tự nâng cấp trả phí.
