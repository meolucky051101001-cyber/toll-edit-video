# Kiểm thử video thực tế sau triển khai — 16/11/2026

## Phạm vi và kết luận

Đã tải video người dùng chỉ định, kiểm tra giải mã đầy đủ và chạy OCR GPU local
cùng FFmpeg NVENC cho bản xem thử chỉ có blur, giữ âm thanh gốc. Video nguồn:
26,2 giây, 720×1280, 30 fps, H.264/AAC, 3.729.095 byte;
SHA256 `02cbe40344736896944ad8c9a659239ec6dbba77ba59c2d1a7ebe0aa474dfab8`.

**Render kỹ thuật đạt; nghiệm thu hình ảnh chưa đạt.** Tại 13,00 giây, phụ đề
chuyển từ câu ngắn sang câu dài giữa hai lần lấy mẫu OCR. Vùng blur của câu cũ
còn hiệu lực, nên lộ ký tự ở hai bên. Bản xem thử không có lời dịch/TTS/phụ đề
tiếng Việt. Giữ `solid_box` làm mặc định; cần tinh chỉnh biên thời gian bằng
quan sát khung hình tại điểm chuyển câu trước khi đổi mặc định sang blur.

Không chạy toàn bộ pipeline dịch/TTS: kiểm duyệt tự động yêu cầu xác nhận riêng
việc gửi văn bản/khung hình tới Gemini và lời dịch tới CapCut. Lượt kiểm thử này
chặn kết nối Python ra host ngoài máy ở cả tiến trình chính và worker, chỉ dùng
model OCR đã lưu local. Không gọi provider, không thuê GPU cloud.

## Hai sửa bổ sung từ kiểm thử

1. Video tạo 29 hình chữ nhật blur khác nhau. Chuỗi split/overlay nối lặp khiến
   FFmpeg khởi tạo rất chậm: riêng một frame không hoàn tất trong 15 giây.
   Đổi sang split nguồn một lần, lấy mỗi crop từ nguồn gốc, rồi overlay theo
   thời gian. Chỉ chạy boxblur trong khoảng cần che. Bản dùng OCR cache render
   cả 26,2 giây trong khoảng 4,6 giây trên máy hiện tại.
2. Không tái dùng kết quả OCR chỉ vì sai khác trung bình của vùng cảnh nhỏ.
   Chữ nhỏ có thể thay đổi mà cảnh gần như đứng yên. Chỉ tái dùng khi toàn bộ
   ảnh đầu vào OCR giống nhau. Sửa này không giải quyết hoàn toàn biên thời gian
   giữa các mẫu OCR, như quan sát ở 13,00 giây nêu trên.

Tăng phiên bản render blur lên 3 và geometry lên 7 để resume/Studio làm lại
cache cũ. Thêm test pixel FFmpeg thực với 30 vùng di chuyển và test chữ nhỏ đổi
trên nền tĩnh. Nhóm kiểm thử liên quan cuối cùng: **127 passed**, không skip,
không lỗi; một cảnh báo audioop deprecated. CI chạy lại trên commit mới.

## Bằng chứng local

Thư mục: `scratch/xhs-3MrohcSsCYV-20261116/` (không đưa media/log vào Git).

- `download.json`: hash và thông tin tải; `decode.json`: giải mã nguồn không lỗi.
- `offline-v2/ocr.json`: 18 cue cuối sau chuẩn hóa, 75 lần OCR theo timeline.
- `offline-v2/boxes.json`: 69 observation vùng che.
- `offline-v2/blur_only.mp4`: bản xem thử local, âm thanh gốc.
- `offline-v2/result.json`: OCR và render hoàn tất trong 23,91 giây (không bao gồm tải).
- `offline-v2/media-validation.json`: probe đầu ra và giải mã toàn file không lỗi.
- `offline-v2/blur-comparison.png`: so sánh các khung hình 1/7/13/20 giây.
- `offline-v2/visual-qa.json`: ghi nhận nghiệm thu hình ảnh chưa đạt.
- `scratch/deployment-tests/video-fixes-final.xml`: kết quả 127 test.

Thời gian trên là quan sát một lần trên máy này, không phải benchmark tổng
pipeline. Chưa đo mức RAM/VRAM cực đại và chưa nghiệm thu đồng bộ giọng dịch.
