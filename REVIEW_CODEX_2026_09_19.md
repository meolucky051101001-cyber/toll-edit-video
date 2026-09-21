# Review 2026-09-19

Source: C:/Users/admin/Projects/ai-video-research-tool.

Đã sửa:
- Chuyển backend 8000 sang 8010 vì cổng 8000 đang được ứng dụng antidetect khác sử dụng. Đồng bộ proxy, launcher, API docs, origin. Frontend giữ 3000.
- Trang chính mặc định tìm Xiaohongshu thật; mock phải chọn rõ.
- Giữ cả nhíp và sticker/mi trong query ghép; ranking yêu cầu bằng chứng cho cả hai thành phần, giữ hành động và giá rẻ. Đây là luật giới hạn phạm vi, không phải hiểu mọi chủ đề.
- Cookie cũ không ghi đè URL yêu cầu login hoặc tourist_search.
- Phân biệt lỗi điều hướng với lỗi khởi chạy Chrome.

Kiểm chứng:
- Baseline 309 tests passed; sau sửa 318 tests passed (3 warnings).
- 54 kiểm thử tập trung và 1 Playwright về default mode qua.
- TypeScript và production build qua; startup backend/frontend và hai downloader thành công.
- Query thật nhíp gắp sticker tạo đúng 贴纸镊子 / 手账贴纸镊子 / 贴纸专用镊子.

Giới hạn:
- Rednote trả ERR_HTTP_RESPONSE_CODE_FAILURE sau khi Chrome khởi chạy, cả /explore lẫn trang chủ. Chưa nghiệm thu tìm và tải video thật. Không thay dữ liệu thật bằng mock.
- Chưa kiểm chứng độ chính xác trên tập dữ liệu lớn.
- Test SSE có cảnh báo EventSubscription.get chưa await; chưa xác định ảnh hưởng runtime.
