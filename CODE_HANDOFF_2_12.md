# Tool V2 2.12.0 — bàn giao mã nguồn

Ngày: 16/09/2026. Phạm vi: hoàn thiện code các đợt cải tiến; người dùng tự nghiệm thu video thực tế.

## Đã sửa

- OCR: đọc đúng thời gian video VFR; bản phân tích nhỏ dùng CUDA/NVENC, không thay đổi độ phân giải video xuất. Chỉ nạp model OCR một lần trong mỗi job và bỏ kiểm tra kết nối máy chủ model không cần thiết khi khởi tạo.
- Phân loại phụ đề: khắc phục trường hợp vùng quét của các đoạn thoại chồng nhau khiến một phụ đề thật bị nhầm thành chữ sản phẩm lặp lại. Vẫn giữ điều kiện đối chiếu lời thoại và vị trí để loại chữ trên bao bì.
- Khung che: căn giữa ngang, bo bốn góc, ổn định kích thước trong cùng vùng phụ đề; không kéo thành khung lớn khi phụ đề đổi vị trí. Không phát sinh sự kiện ASS dài 0 giây. Giữ cơ chế nối khung và giữ tối đa một giây sau vùng chữ nguồn.
- Lấy ảnh ngữ cảnh dịch bằng giải mã GPU theo timestamp; không seek lặp nhiều lần trên video 4K. Giữ ảnh gửi kèm Gemini.
- Dịch: giới hạn số lần thử và timeout, cooldown khi dịch vụ lỗi; không ghi API key vào URL gọi Gemini. Giữ các cơ chế dự phòng hiện có.
- Đo thời gian: OCR và dịch được ghi nhận hoàn thành riêng, không tính thời gian của bước chậm vào cả hai bước. Cache OCR hợp lệ không phải chạy lại bước dò sơ bộ.
- Render: giải mã CUDA, mã hóa NVENC; không tự chuyển sang encoder CPU khi GPU lỗi. Các thao tác phụ trợ như bố trí phụ đề/âm thanh vẫn có phần chạy CPU, không phải toàn bộ ứng dụng chạy trên GPU.
- QC: lấy PTS thật của ảnh kiểm tra, dùng đúng khoảng thời gian ASS `[start, end)`, chặn giao kết quả khi thiếu ảnh kiểm tra ở chế độ strict, xử lý được trường hợp không có ASS.
- Tách lịch sử render V2 khỏi V1; metadata phát hành dùng kết quả test thực thay cho số lượng test viết cố định.

## Kiểm tra mã nguồn

- 102 bài test hồi quy liên quan: PASS, 6.654 giây. Log: `workspace/repair-20260915-final/code-handoff-tests-02.log`.
- Bộ test đầy đủ ở lần kiểm tra trước: 312 PASS; sau đó bổ sung kiểm tra biên QC và kiểm tra thiếu ảnh. Không gọi kết quả cũ là kết quả toàn bộ bộ test mới.
- Kiểm tra môi trường trước bàn giao: 36 PASS, không cảnh báo/lỗi.
- Không chạy thêm pipeline/render thử video sau yêu cầu người dùng tự kiểm thử.
- Tool V1 không được chỉnh sửa hoặc khởi động lại. Các thay đổi giao diện/canvas có sẵn của người dùng được giữ nguyên.

## Trạng thái vận hành

Code đã sẵn sàng để khởi động và người dùng kiểm thử. Bản đang chạy cần khởi động lại để nạp code mới; đợt bàn giao code này không tự khởi động bot hoặc nhận/xử lý hàng đợi. Điểm vào hiện có: `start_bot.bat`.

Không coi đây là chứng nhận mọi video đã đạt. Độ chính xác hình ảnh, giọng đọc và tốc độ trên video mới do người dùng nghiệm thu. Lỗi mạng/quota của nhà cung cấp vẫn có thể xảy ra; code giới hạn thời gian chờ chứ không thể bảo đảm API luôn thành công.

## Bảo toàn dữ liệu

Không xóa video, model hoặc kết quả cũ. `RELEASE_METADATA.json` ghi vị trí bản sao lưu source mới và bằng chứng test; `RELEASE_MANIFEST.sha256` dùng để kiểm tra tính toàn vẹn bản bàn giao. Không tự khôi phục cả thư mục backup lên source vì có thể ghi đè thay đổi mới của người dùng.
