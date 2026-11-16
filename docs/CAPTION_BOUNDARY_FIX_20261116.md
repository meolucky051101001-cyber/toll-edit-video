# Sửa vùng blur tại điểm chuyển câu — 16/11/2026

## Kết quả

Đã sửa lỗi hở chữ nguồn trên video mẫu tại **13,000 giây** và lỗi lệch một
khung hình tại **16,167 giây**. Bản lồng tiếng mới đã qua QC tự động, kiểm tra
giải mã toàn file và đối chiếu hình ảnh tại hai điểm này. Đây là nghiệm thu
video mẫu 720×1280, 30 fps; chưa chuyển `localized_blur` thành mặc định chung.

## Thay đổi

- OCR vẫn lấy mẫu ban đầu mỗi 0,35 giây. Khi chữ hoặc vị trí chữ khác nhau ở
  hai mẫu, quét thêm khoảng giữa đến khi xác định được hai khung hình kề nhau
  ở chỗ thay đổi. Giữ quan sát không có chữ để không nối vùng che qua khoảng trống.
- Vùng che dùng thời gian xuất hiện của chữ nguồn; không kéo theo thời lượng
  TTS. Điểm bắt đầu/kết thúc của observation đã xác minh được giữ qua tracking.
- Filter FFmpeg ghi mốc thời gian xuống đơn vị microsecond thay vì làm tròn lên.
  Trước đây `5/30` thành `0.166667` khiến khung hình đầu câu mới còn dùng mask cũ.
- QC giữ mẫu giữa câu và thêm mẫu trước/tại/sau biên chữ nguồn, dùng vùng kiểm
  tra bao cả câu cũ và mới. Tối đa 192 mẫu biên; báo WARN khi bị giới hạn hoặc
  không giải mã đủ, UNKNOWN khi OCR trả thiếu kết quả. Không coi lấy mẫu thiếu là PASS.
- Geometry version **8**, blur render version **4**: resume và Studio bỏ cache
  cũ. Module `visual_caption_timing.py` là file bắt buộc trong gói release.

Refinement giới hạn 4.000 khung hình bổ sung và dừng có lỗi nếu vượt ngân sách.
Hai mẫu đầu/cuối giống nhau vẫn dùng khoảng lấy mẫu gốc: đây không phải quét
mọi khung hình của mọi video. Caption rất ngắn xuất hiện rồi mất hoàn toàn giữa
hai mẫu giống nhau vẫn cần bộ mẫu riêng trước khi rollout rộng.

## Kiểm chứng

- **171 test đạt**, không skip, không lỗi, hai cảnh báo deprecation đã có.
  Có test chuyển câu ngắn sang dài, khoảng không có chữ, ngân sách/cancel,
  pixel FFmpeg thực ở 10/30 fps, QC câu ngắn, thiếu kết quả OCR, resume/Studio
  và gói thiếu module bắt buộc.
- Một lượt test chạy cùng pipeline GPU bị native access violation trên Windows
  khi RAM trống thấp. Chạy lại riêng sau khi pipeline thoát: 171/171 đạt trong
  19,79 giây. Chưa đủ bằng chứng kết luận nguyên nhân crash.
- QC mới quét bản blur cũ: **WARN**, phát hiện nhiều điểm hở chữ mà bốn mẫu
  giữa câu trước đây bỏ sót. Lượt kiểm tra tiếp theo còn phát hiện sai số timestamp
  ở 16,167 giây; đã thêm test 30 fps và sửa trước lượt end-to-end cuối.
- Lượt end-to-end mới: Gemini dịch và CapCut Mai tạo đủ **18 câu**, hoàn tất
  trong **194,28 giây**. Có 75 mẫu OCR ban đầu và 155 khung hình refinement.
- QC cuối: **15 PASS**, không warning/failure; kiểm tra **143 khung hình**
  (139 mẫu biên + 4 giữa câu), không bị cap, không phát hiện chữ nguồn lộ ra.
- Đầu ra: H.264/AAC, 720×1280, 30 fps, 26,17 giây, 11.042.234 byte.
  Giải mã toàn file không lỗi. Đã đối chiếu các frame 388–391 và 484–487.
- Lượt mới chậm hơn lượt 124,62 giây trước do OCR refinement và QC lấy nhiều mẫu.
  Thời gian là quan sát một lần, không phải benchmark cố định. Chưa chứng nhận
  lip-sync bằng nghe/xem toàn bộ hoặc tất cả kiểu video nguồn.

## Bằng chứng local

Trong `scratch/xhs-3MrohcSsCYV-20261116/` (media/log không commit):

- `boundary-v1/ocr.json`, `boxes.json`: vùng nguồn sau refinement.
- `boundary-v1/qc-offline-v2.json`: QC phát hiện lỗi trên bản cũ.
- `boundary-v1/qc-boundary-v1.json`: phát hiện sai số timestamp trước lần sửa cuối.
- `boundary-full/acceptance.json`: kết quả end-to-end và QC cuối.
- `boundary-full/output/localized_blur.mp4`: bản lồng tiếng mới.
- `boundary-full/output/validation.json`: probe, hash và giải mã toàn file.
- `boundary-full/output/transition-comparison.png`: nguồn / bản cũ / bản mới.
- `boundary-full/output/visual-qa.json`: xác nhận quan sát các khung hình lỗi cũ.
- `scratch/deployment-tests/boundaries-sequential.xml`: kết quả 171 test.

SHA256 thành phẩm:
`8a50d61aa1a38f5002eddc6e8769244a2a3808ed0690d98579996a9204ed9153`.

Người dùng đã cho phép Gemini/CapCut trong phiên trước; lượt này giữ đúng hai
provider đó. Các bản thành phẩm trước được giữ nguyên để so sánh.
