# Đối chiếu ASR/OCR/bản dịch Local — 16/11/2026

Làm việc tại `C:\tool v1`, nhánh `tool-v1`, HEAD
`4b2d234acc92fead3fc097e570de523089290aa4`. Đã kiểm tra status, diff, hồ sơ
phiên trước và năm job Local mới nhất lúc bắt đầu rà (10:48–11:05, Asia/Bangkok).

**Kết quả: mã sửa và kiểm thử offline đạt; chưa chốt nghiệm thu thành phẩm
Local sau sửa.** Có 16 câu dịch được chỉnh trong bản SRT riêng. Audio/video
đã phát trước đó vẫn mang nội dung cũ. Cần tái tạo TTS/render và nghe/xem
thành phẩm mới trước khi đánh dấu nghiệm thu cuối.

## Phân biệt với hồ sơ phiên trước

Phiên trước đã khép ASR/đồng âm/dịch cho ba mẫu Cloud tại
`C:\tool_v1_clean\cloud-phase0-1-20261114`, session `cjob07db8a2aa1`.
Kiểm chứng lại chỉ đọc: 3/3 output SHA256, 3/3 identity nguồn/cue/voice/timing,
9/9 acceptance report và 11/11 file code/config Canary khớp. Tổng 59 câu;
giữ kết quả 6/6 tiêu chí và drift nhóm 0,201 giây trong phạm vi đó.

`phase7_closure.json` còn khớp 33/34 liên kết: tài liệu
`CLOUD_PHASE7_ACCEPTANCE_AND_CANARY_20261114.md` đã đổi sau khi đóng receipt.
Không sửa receipt cũ. Dữ liệu thành phẩm và chín báo cáo chuyên môn vẫn khớp.
Đây không phải chứng nhận cho runtime Local hoặc cho mọi video mới.

## Lỗi Local đã tái hiện và sửa

1. OCR thay cả câu theo độ tương đồng chữ Hán, làm hỏng số/Latin hoặc đưa
   lỗi nhận dạng hình ảnh vào ASR đúng: `90年代 → 9日年代`,
   `这样/损伤 → 这释/损份`, `尺寸 → R寸`.
   Guard mới giữ số/Latin, dấu câu và số token; chỉ thay chữ CJK cùng vị trí.
   Thay từ hai chữ trở lên cần caption trùng ở ít nhất hai thời điểm cách nhau
   ≥0,25 giây. Không dùng lại best row ngoài cue sau khi lọc thời gian.
   `ocr_text_review` lưu ASR/caption/quyết định/lý do trong payload V1.
   Đây là chốt bảo thủ, không phải bộ xác minh ngữ âm tổng quát.
2. Metadata dịch chỉ mang một best frame, làm mất caption thay đổi trong
   cùng cue. `v1_caption_evidence.py` giữ thứ tự các caption đã chọn, loại
   lặp liền nhau, chữ bao bì/watermark/ngoài cue, giới hạn 320 ký tự.
   Hints đến cả Gemini, OpenAI và DeepSeek; giữ ID, speaker, budget và
   ngữ cảnh của caller. Prompt yêu cầu giữ hình dạng/số lượng/điều kiện/thao tác
   và không xem OCR là chân lý. Thay prompt tự đổi cache key vốn chứa prompt.
3. Replay trên dữ liệu thật giữ `90`, `这样/损伤`, chặn `R寸`, vẫn sửa
   `纸片骨 → 纸片谷`. `新芯片 → 星星片` và `眉眉 → 美美` của mẫu này
   được rà bằng ảnh nguồn; guard tự động giữ ASR khi caption thiếu token hoặc
   chứa lỗi ASCII. Không hạ ngưỡng để ép những trường hợp này thành tự động PASS.

## Bản dịch đã đối chiếu

77 câu được rà qua ASR gốc, OCR theo thời gian, bản dịch và bản rút gọn TTS.
27 cue được xem thêm ở hai khung nguồn mỗi cue (54 ảnh). Bốn đoạn âm thanh
gốc được nhận dạng lại bằng SenseVoice CPU ONNX; không gọi dịch vụ mạng.
Agent không nghe trực tiếp và không coi ASR thứ hai là ground truth.

| Job prefix | Câu | Cuối ASR / video (s) | Câu dịch chỉnh |
|---|---:|---:|---|
| `ab5f6a74` | 11 | 31,36 / 31,567 | 4, 8, 10, 11 |
| `d8f98d49` | 25 | 81,18 / 81,50 | 2, 13, 16, 23, 24, 25 |
| `13e37a8a` | 8 | 28,12 / 28,567 | 2, 3, 6, 7 |
| `e7c5450b` | 20 | 62,60 / 63,096 | 5, 12 |
| `8787c0ae` | 13 | 32,26 / 34,551 | Không thay |

Các sửa chính: badge hình vuông thay vì chữ nhật (sản phẩm đầu); miếng
trang trí hình ngôi sao thay vì kim tuyến chung; giữ cả hình dạng và kích cỡ;
giữ tùy chọn kéo hai bên, điều kiện không đậy nắp, vật mua là màng/bao card;
không dịch `耐看` thành độ bền vật liệu; giữ phụ kiện/ruy băng được tặng.

`d8f98d49` câu 23 dùng tên **Thâm Không** (`深空`) và hình chữ nhật theo
caption có chú thích `说错了` (nói nhầm). ASR bổ sung đọc hình vuông, nên đây
là sửa biên tập theo caption đính chính, không tuyên bố trùng nguyên văn âm thanh.
Tên Donglin Acrylic có căn cứ `东林亚克力` trên hai ảnh; không tự sửa tên này.
Chuỗi 1,2,3,4,5,6,8,9,10,12 được caption xác nhận; không thêm số 7/11.

Đoạn cuối `8787c0ae` không có caption ở hai ảnh đã xem. SenseVoice nhận lại
được `持续欣赏...`, hỗ trợ nghĩa chung “tiếp tục ngắm”; giữ bản dịch hiện tại.
Không thêm câu mới ở đuôi video, không dùng khoảng thời gian không có cue
để tự kết luận mất lời hoặc chỉ có nhạc.

## Kiểm chứng

- **140 passed, 28 skipped, 1 deselected**, exit 0, Python 3.12.14.
  28 skip có sẵn trong `tests/conftest.py`; deselect là test tải/infer ONNX
  toàn mẫu, độc lập với bốn inference CPU bổ sung đã thực chạy.
  Có một cảnh báo `audioop` deprecated của pydub.
- Bộ chạy gồm OCR correction/acquisition/reuse, caption metadata và adapter
  dịch thật qua HTTP mock, quality gate, resilience/cache, speech repair,
  ASR timing/cache/isolated, orchestrator golden và checkpoint.
- Lượt sandbox bị treo ở socketpair của `asyncio`, tái hiện bằng stdlib không
  import dự án. Bộ trên đạt sau khi chạy ngoài giới hạn socket; không sửa test
  hoặc mock event loop để né lỗi. Các lần chạy dở không được tính PASS.
- 5/5 video nguồn giải mã toàn bộ; 77/77 cue giữ ID/thời gian, không rỗng,
  không sót CJK trong bản Việt, mốc nằm trong thời lượng nguồn.
- 77/77 file TTS **cũ** còn tồn tại, giải mã được, duration khớp metadata
  trong dung sai 0,1 giây; tốc độ ghi nhận lớn nhất 1,293801×.
  Kiểm tra này không chứng nhận audio cho 16 câu mới sửa.
- `git diff --check` đạt. Không commit/push, không thay runtime artifacts,
  không khởi động lại bot, không gửi Telegram, không chạy Cloud/provider trả phí.

## Artifact và bước nghiệm thu tiếp

Toàn bộ evidence ở `C:\tool v1\scratch\asr_review_20261116` (Git-ignored):

- `before/`: snapshot file trước lượt sửa và diff của working tree.
- `input_manifest.json`: đường dẫn thật, bản sao và SHA256 input/runtime artifacts.
- `telegram_*/review.json`: so sánh ASR gốc → OCR cũ → guard mới → bản dịch đã rà.
- `telegram_*/translated.reviewed.srt`: năm bản ứng viên, giữ timing gốc.
- `reviewed_changes.json`: 16 chỉnh sửa và lý do/bằng chứng theo cue.
- `acceptance.json`: kết quả cấu trúc, hash và giới hạn; không phải release PASS.
- `existing_tts_audit.json`, `supplemental_audio_review.json`: audit audio cũ,
  kết quả ASR bổ sung và hash bốn clip gốc.
- `final-tests.log/.xml`: kết quả hồi quy cuối.

Các chỉnh sửa source hiện chỉ có trên đĩa; tiến trình đã import module cần
nạp lại trong cửa sổ idle. Các job đã checkpoint sẽ vẫn dùng nội dung cũ nếu
không tạo revision/reprocess đúng cơ chế. Bản dịch mới cần TTS, fit duration,
ASS/render và QC theo chính artifact mới; không gắn receipt cũ sang bản mới.
Chưa tuyên bố nghiệm thu hình ảnh/giọng đọc hay timing từng từ cho bản sửa.
Các lỗi geometry còn mở ở bàn giao 14/11 không thuộc phạm vi đã sửa lần này.
