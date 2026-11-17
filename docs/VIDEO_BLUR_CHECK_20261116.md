# Kiểm thử video thực tế sau triển khai — 16/11/2026

> Kết quả mới nhất: lỗi chuyển câu đã được sửa và video mẫu chạy lại đạt QC
> 143 khung hình. Xem [báo cáo sửa biên caption](CAPTION_BOUNDARY_FIX_20261116.md).
> Các kết quả chưa đạt bên dưới là bằng chứng của các lượt trước khi sửa.

> Cập nhật: sau xác nhận của người dùng, đã chạy đủ Gemini và CapCut. Kết quả
> toàn pipeline ở cuối tài liệu; phần đầu ghi lại lượt test local trước đó.

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

Thời gian trên là quan sát một lần của lượt local, không phải benchmark tổng
pipeline. Lượt này chưa đo RAM/VRAM cực đại hoặc nghiệm thu đồng bộ giọng dịch.

## Lượt toàn pipeline sau xác nhận Gemini và CapCut

Người dùng đã xác nhận cho phép gửi văn bản/khung hình tới Gemini và lời dịch
tới CapCut, sử dụng API đã cấu hình. Lượt chạy dùng mã commit `b53b3d5`, thư mục
test riêng và `localized_blur`; không bật dịch vụ production hoặc cloud GPU.
Các fallback LLM ngoài phạm vi xác nhận bị tắt trong runner của lượt test.

- Toàn pipeline hoàn tất trong **124,62 giây**, gồm tách nhạc BS-RoFormer,
  ASR hybrid, OCR, Gemini, CapCut, trộn âm, NVENC và QC.
- Dùng 18 cue OCR sau đối chiếu với ASR; Gemini dịch đủ 18 câu.
- CapCut Mai (`BV562_streaming`) tạo đủ 18 audio, QC không phát hiện đè câu.
- NVENC render trong **6,05 giây**. Đầu ra 720×1280, 30 fps, H.264/AAC,
  26,17 giây, 11.150.639 byte. Giải mã toàn bộ file không có lỗi.
- QC tự động: **15 PASS, 0 warning, 0 failure**, policy `BLOCK`.
- Quan sát tài nguyên: RSS cộng dồn cây tiến trình cao nhất **2.705,6 MB**
  theo sampler mỗi giây; bộ nhớ GPU toàn hệ thống cao nhất **3.095 MiB**
  theo mẫu mỗi ba giây. Đây không phải RAM riêng biệt hay VRAM riêng của job;
  phép lấy mẫu có thể bỏ lỡ đỉnh ngắn.

**Nghiệm thu hình ảnh vẫn chưa đạt:** ở 13,00 giây còn hở chữ nguồn hai bên
vùng blur. QC pixel chỉ lấy mẫu tại 0,607 / 8,561 / 15,403 / 24,930 giây nên
bỏ sót điểm này. PASS tự động không thay thế việc kiểm tra biên chuyển câu.
Chưa xác minh lip-sync bằng nghe/xem toàn bộ; QC chỉ kiểm tra thời lượng và
va chạm giữa các câu. Không đổi blur thành mặc định.

Bằng chứng trong cùng thư mục test:

- `acceptance.json`: kết quả tổng pipeline và 15 kiểm tra QC.
- `output/localized_blur.mp4`: bản tiếng Việt có giọng CapCut và phụ đề.
- `output/validation.json`: probe, giải mã toàn file, SHA256 và mẫu tài nguyên.
- `output/comparison.png`: so sánh hình nguồn và thành phẩm tại 1/7/13/20 giây.
- `output/visual-qa.json`: ghi nhận lỗi còn lại dù QC tự động PASS.
- `workspace/acceptance_job/translated.srt`: phụ đề tiếng Việt.
- `workspace/acceptance_job/voice_lock.json`: giọng khóa theo job.

SHA256 đầu ra:
`de70d991765e143314f6ea3ae7142cf56328c3f4bebe2356ff12c453574b74e0`.
Sau test, các worker đã thoát; giữ nguyên downloader độc lập và trạng thái
dịch vụ Tool V1 đang dừng.
