# Đối chiếu nội dung và timing trước Giai đoạn 8 — 15/11/2026

Checkout: `C:\tool_v1_clean\cloud-phase0-1-20261114`.
**Cập nhật chốt 16/11/2026: đã khép các lỗi đã xác minh trên ba mẫu.**
Job mới `cloud-20261115-232507-db8a2a`, session `cjob07db8a2aa1`, hoàn tất
59/59 câu CapCut TTS. Đối chiếu mới đạt sáu tiêu chí, ranh giới nhóm lệch
tối đa **0,201 s**. Người dùng xác nhận bốn đoạn thành phẩm nghe rõ và yêu
cầu giữ cách đọc hiện tại. Chi tiết kết quả, bằng chứng và giới hạn ở mục 7.

Mục 1–6 bên dưới giữ nguyên lịch sử phát hiện trên job cũ
`cloud-20261115-122853-212130`; các kết luận FAIL tại đó không phải trạng
thái của thành phẩm mới. Giai đoạn 7 đủ điều kiện bàn giao sang triển khai
Giai đoạn 8; chưa bật phát hành Cloud, `V1_CLOUD_MODE=False`.

## 1. Video ngắn: đối chiếu theo nội dung thay cho ID

Dùng tiếng Trung cuối trước dịch: Local lấy `translation.json.orig_content`,
Cloud lấy `subtitles.srt`. Rà bản Việt sau khi ghép. Script
`scratch/asr_content_alignment/compare_short.py` chuẩn hóa Unicode, bỏ dấu
câu/khoảng trắng, căn nhóm liên tiếp bằng khoảng cách chỉnh sửa ký tự.
Giới hạn nhóm 3 Local/4 Cloud, có nhánh ghi nhận thiếu/thừa và phạt gộp.
Không dùng timestamp để chọn cặp, tránh tối ưu đường căn chỉ để drift nhỏ.
Ngưỡng tương đồng 0,45 chỉ tìm ứng viên để rà, không phải ngưỡng PASS nội dung.

| Cue Local | Cue Cloud | Khoảng Local (s) | Khoảng Cloud (s) | Max lệch ranh giới |
|---|---|---|---|---:|
| 1–2 | 1–2 | 0,080–6,000 | 0,000–6,059 | 0,080 s |
| 3 | 3–4 | 6,260–8,940 | 6,059–9,080 | 0,201 s |
| 4 | 5–6 | 9,680–11,940 | 9,519–12,039 | 0,161 s |
| 5 | 7 | 12,980–16,080 | 12,859–16,199 | 0,121 s |
| 6 | 8–10 | 16,340–20,040 | 16,199–20,120 | 0,141 s |
| 7 | 11–12 | 20,120–23,400 | 20,120–23,480 | 0,080 s |
| 8 | 13–14 | 23,480–26,260 | 23,480–26,199 | 0,061 s |

Cả 8 cue Local và 14 cue Cloud nằm trong bảy nhóm; bốn nhóm khớp nguồn
hoàn toàn sau chuẩn hóa. Không thấy một nhóm ý hoàn chỉnh bị bỏ/thêm chỉ
vì số cue khác nhau. Drift 9,152 s của phép ghép ID không mô tả đúng các
câu tương ứng. **Ranh giới ngoài của nhóm** đạt ngưỡng 0,5 s cũ.

Điều này không chứng nhận timing từng từ/TTS: baseline Local không có
word timestamps tương ứng, ASS render có thể khác SRT. Bộ so sánh chính
thức và `parity.json` cũ giữ FAIL; không ghi đè hoặc tự mở cổng nghiệm thu.

## 2. Sai khác nội dung còn lại

Đã rà đủ bảy nhóm nguồn/VI và xem trực tiếp frame nguồn 2,865 s, 6,930 s,
10,245 s. Các điểm cần sửa:

| Khoảng | Bằng chứng nguồn | Đầu ra Cloud | Kết luận |
|---|---|---|---|
| 9,519–10,820 s | Caption `裁剪也特别舒适`, hình dùng kéo cắt xốp | Source `材质也特别舒适`, VI “Chất liệu sờ vào cực kỳ thích luôn” | Đổi ý **cắt** thành **chất liệu/sờ** |
| 6,059–7,740 s | Caption `而且也是挺大一卷的` | Source `而且也是挺塌一卷呢`; VI thêm “dày dặn” | Sai chữ `大` → `塌`, có diễn giải thêm |
| Đầu video | Caption `了个位数的青色气泡膜` | VI “giá chỉ vài nghìn” | Nguồn nói giá một chữ số; chưa có căn cứ quy đổi đơn vị “nghìn” |

Caption là bằng chứng hình, không tự động là transcript chuẩn của lời nói.
Các khác biệt này vẫn đủ để chưa chứng nhận tương đương nội dung. Local
cũng sai đoạn đầu (`各位是你亲自去泡沫`); bản VI thiếu màu xanh/giá, thêm
lời gọi “Mọi người ơi”. Không coi Local là chân lý tuyệt đối. Bốn nhóm cuối
giữ các ý loại rộng 20 cm, gói đồ/tiểu thuyết, các lớp xốp và đẹp mắt khi
đối chiếu văn bản. Chi tiết từng nhóm nằm trong `alignment.md`/`.json`.

## 3. Đoạn 30,4 s đã có xác nhận nghe

Trích nguyên âm thanh nguồn **28,5–34,0 s** thành WAV mono 16 kHz,
không lọc/thay tốc độ, gửi người dùng nghe. Với câu hỏi có cụm lời riêng
ngay trước `完美` quanh 30,4 s hay không, người dùng trả lời:

> Không có, chỉ nhạc/tiếng động

Đây là xác nhận nghe của **người dùng**, không phải agent đã nghe: công cụ
trả rõ không hỗ trợ audio input cho phần phân tích. Receipt lưu nguyên văn
trả lời, khoảng cắt, SHA256 clip/nguồn và phạm vi trong
`scratch/asr_content_alignment/listening_review.json`.

**Không tính khoảng này là thiếu lời ASR**; không chèn câu từ caption hoặc
bản giải mã thử. Câu `完美` hiện có mốc 31,22–31,78 s. Xác nhận chỉ áp dụng
cho đoạn nghi vấn, không chứng nhận mọi lời hoặc timing thành phẩm.

## 4. Lỗi timing thực trên video dài — P1 chặn nghiệm thu

Cue ASR/SRT đầu dài **0–27,46 s**, chứa nhiều câu. ASS chia bản dịch thành
nhiều trang nhưng toàn bộ chữ Việt/hộp che của cue kết thúc ở **19,89 s**.
Không có event ASS nào trong **19,89–31,22 s** (11,33 s). Phần
19,89–27,46 s vẫn thuộc cue có lời, khác khoảng nghỉ sau cue.

Trang Việt cuối “Cứ ấn vào theo cách của tôi, chỗ thừa thì gập lại” hiện
**16,90–19,89 s**. Raw word timestamps đặt `按我的手法按进飞机盒` ở
**23,48–25,12 s**, `多了往里折折` ở **26,52–27,46 s**. Hai đầu của
nhóm ý tương ứng đi trước khoảng **6,58–7,57 s**. Mốc từ là phép đo decoder;
bằng chứng hình độc lập xác nhận lỗi hiển thị:

**Tại giây 25, đầu ra Cloud lộ nguyên chữ Trung `按我的手法按进飞机盒`,
không có hộp che hoặc phụ đề Việt**, giống frame nguồn. Đã xem trực tiếp
`scratch/asr_content_alignment/long_25_source.png` và `long_25_cloud.png`.
“20/20 cue tìm được hộp OCR” không đồng nghĩa che đúng suốt thời gian nguồn.

Đường đi gây lỗi:

1. Nhiều câu nằm trong một cue ASR dài, dịch/TTS chung một lượt cho cue.
2. `build_ass_subtitles_v1` trong `backend/cloud/gpu_scripts/full_pipeline.py`
   dòng 1343, lời gọi dòng 1397, truyền thời lượng TTS vào hàm đồng bộ.
3. `sync_and_clamp_subtitles` trong `backend/ass_utils.py` dòng 292,
   nhánh 309–323, thay end bằng `start + actual_duration` kể cả ngắn hơn
   nguồn; tracking bị kẹp và trang chữ được chia trên khoảng đã thu ngắn.
4. Mixer chỉ đặt audio tại start mỗi cue, không có mốc các câu con để giữ
   nội dung theo hành động trên hình.

Bundle không lưu từng file TTS Cloud hoặc duration riêng trong metadata.
Chưa kết luận timing từng từ của audio/overlap đã được đo. Lỗi ASS/hộp che
đã được chứng minh trực tiếp và đủ chặn nghiệm thu. Video ngắn không có
gap dài tương tự; giữ một trang đến cue sau không tự thân là lỗi. Local
cũng thay end theo TTS, nên phải kiểm tra thành phẩm riêng.

## 5. Quyết định và phần cần sửa trước Giai đoạn 8

| Hạng mục | Kết luận |
|---|---|
| Chia câu video ngắn | Đã giải thích bằng bảy nhóm; max lệch ranh giới 0,201 s |
| Nghi vấn thiếu lời 30,4 s | Đã khép theo xác nhận nghe của người dùng |
| Tương đương nội dung | Chưa đạt: cắt/chất liệu, sai chữ, diễn giải giá chưa có căn cứ |
| Timing/che phụ đề video dài | Không đạt: kết thúc sớm, lộ chữ Trung tại giây 25 |
| Giai đoạn 8 | Tiếp tục đóng; `V1_CLOUD_MODE=False` |

Phần sửa tiếp cần xử lý cơ chế chung: chia cue dài theo mốc từ/câu trước
dịch/TTS; giữ thời gian che caption nguồn độc lập với độ dài giọng đọc;
thêm QC trên ASS thực; rà sửa ASR bằng OCR và dịch không tự thêm giá/đơn vị.
Không hard-code mốc/câu mẫu. Sau sửa cần kiểm chứng lại đầu ra có lỗi,
giữ CapCut TTS và ngân sách có giới hạn.

## 6. Bằng chứng và kiểm chứng cục bộ

Các tệp trong `scratch/asr_content_alignment/` đều bị Git bỏ qua:

- `compare_short.py`, `alignment.json`, `alignment.md`: ghép câu và hash
  nguồn. Bốn kiểm chứng split/merge, bỏ câu, câu lặp, dịch thời gian đạt.
- `timing_audit.py`, `timing_audit.json`, `timing_audit.md`: tái hiện gap
  ASS 11,33 s, không event tại 25 s, lời nguồn còn sau khi ASS hết.
- `listening_review.json`, `long_28p5_34_original.wav`: xác nhận nghe,
  clip và hash nguồn không đổi.
- `short_source_*.jpg`, `long_25_source.png`, `long_25_cloud.png`: ảnh đã xem.

Chạy hai script bằng `C:\tool v1\backend\venv\Scripts\python.exe` từ
checkout có thẩm quyền. Không gọi provider/GPU, không stage/commit.
**207 passed, 1 skipped** trong báo cáo trước là hồi quy pipeline trước
lượt đối chiếu này; test đó chưa bắt lỗi hình ảnh mới và không thay nghiệm thu.
Audit GPU gần nhất ghi giảm 3,26 CU/trần 5 CU, không phiên hoạt động;
lượt đối chiếu cục bộ này không tạo phiên mới.

## 7. Khép lỗi bằng Canary mới — 16/11/2026

Job `cloud-20261115-232507-db8a2a` chạy đủ ba mẫu trong phiên T4
`cjob07db8a2aa1`; tất cả pipeline returncode 0, job completed, cleanup
verified. Thành phẩm dưới `scratch/canary_run_20261114/asr_fix/cjob07db8a2aa1/`.

| Mẫu | Câu dịch / CapCut TTS | Thời lượng output | Tốc độ fit lớn nhất |
|---|---:|---:|---:|
| Ngắn | 14 / 14 | 27,200 s | 1,490526× |
| Tên Unicode/ký tự đặc biệt | 15 / 15 | 32,633 s | 1,374046× |
| Dài | 30 / 30 | 59,833 s | 1,373793× |

### Sửa cơ chế và kết quả thực

- Chia câu bằng word timestamps, khoảng nghỉ và mệnh đề trước dịch/TTS;
  giữ text/lineage, không nội suy mốc hoặc ép số câu bằng Local. Video dài
  không còn một cue chứa cả đoạn 0–27,46 s.
- Chữ Việt và vùng che caption nguồn giữ timeline độc lập với độ dài TTS.
  QC đọc lại ASS đã phát ra để kiểm tra text, khoảng phủ và vùng che thực.
  Tại giây 25 đã thấy “Ấn vào hộp theo cách của tôi”; giây 52 đủ “Kéo thẳng,
  ấn vào rồi kéo thẳng”, không còn lỗi mất prefix/lộ thoại nguồn đã ghi nhận.
- Sửa ASR bằng bằng chứng caption lặp ở nhiều frame; chỉ chấp nhận thay chữ
  CJK có căn cứ, giữ prefix/suffix, số và Latin. Khép sai `大`, `裁剪`,
  `拍`, `欧雅`, `韩素纸` và prefix `拉直` khi caption xuất hiện trễ.
- Bản dịch giữ giá một chữ số và màu xanh, không tự thêm “nghìn”/tiền tệ;
  mẫu Unicode không còn tự thêm “Hôm nay”. Rà đủ 59 câu VI, gồm các câu
  rút gọn sau TTS; không thấy sai ý chính về tên, số lượng, thao tác/thứ tự.
- TTS fit dùng WAV PCM để tránh padding khi mã hóa MP3; có dự phòng biên,
  giới hạn tốc độ 1,5× và kiểm tra duration sau fit. Không cắt cụt lời,
  không đổi CapCut, không dùng fallback hoặc BGM-only. Retry API có giới
  hạn; lỗi quota/model không được đổi thành báo cáo PASS.

### Xác nhận của người dùng

Giữ xác nhận cũ “Không có, chỉ nhạc/tiếng động” cho khoảng 30,4 s của
nguồn; không chèn lời giả. Với bốn clip mới (ngắn 0–6,15 và 12,8–16,3 s;
Unicode 0–2,9 s; dài 31,2–37,8 s), người dùng trả lời:

> Nghe rõ, giữ cách đọc hiện tại

Giữ cách đọc merch, Kraft/size, Ouya/Hansu và câu trong clip đã nghe.
Receipt `scratch/content_fix_20261115/human_listening_final.json` lưu
nguyên câu hỏi/trả lời, khoảng cắt, hash clip/output/nguồn. Agent không
nghe trực tiếp; không suy rộng thành người dùng đã nghe toàn bộ ba video.

### Đối chiếu và bằng chứng

`parity_reviewed_short.json/.md` trong session mới đạt **6/6**. Reference
được đọc độc lập từ caption nguồn, ghép với ranh giới ngoài của bảy nhóm
Local: 8 cue Local ↔ 14 cue Cloud, max drift **0,201 s < 0,5 s**. Không dùng
candidate để tạo reference, không ghép bằng timestamp, không nới ngưỡng.
Baseline Local có lỗi nội dung đã biết, nên kết quả là đối chiếu nội dung
nguồn đã rà và ranh giới nhóm Local; không phải khẳng định hai bản render
hay timing từng từ giống nhau. Vai giọng chỉ được chứng minh trên mẫu một giọng.

Mỗi mẫu có `acceptance_evidence/{translation_review,rendered_timing,tts_timing}.json`
gắn SHA256 nguồn, output và snapshot cue gồm voice. Review hình đã xem
contact sheet/mốc lỗi; audit ASS bao phủ mọi cue. Tất cả 59 file TTS được
hash, giải mã, đo duration và đối chiếu biên/tốc độ; hai ASR đọc lại mixed
audio chỉ hỗ trợ rà soát, không coi lỗi nhận loanword/nhạc là ground truth.
Không chứng nhận timing từng từ hoặc mọi frame chưa được xem trực tiếp.

Các artifact bổ sung dưới `scratch/content_fix_20261115/`:

- `cjob07_structural.json`: cả ba mẫu đạt hash, full decode nguồn/output,
  SRT/pipeline binding, ASS và audio audit.
- `semantic_review_final.json`: 45 câu Unicode/dài đối chiếu nguồn độc lập;
  hai sắc thái diễn đạt nhỏ được ghi rõ, không làm sai thao tác chính.
- `reviewed_source_draft.json`, `reviewed_short_reference.json`: nội dung
  tham chiếu mẫu ngắn và nguồn gốc ranh giới Local.
- `final_code_verification.json`: toàn bộ 11 file trong receipt trước
  Canary khớp byte hiện tại; giữ bản mà người dùng đã nghe.
- `phase7_closure.json`: quyết định bàn giao có hash bằng chứng; không phải
  cờ bật Cloud hoặc chứng nhận Giai đoạn 8 đã hoàn tất.

Hồi quy đã chạy **324 passed, 1 skipped** trước Canary. Mock Telegram được
sửa sau đó, kiểm tra riêng **1 passed** với RuntimeWarning thành lỗi,
không warning. Không thay production sau Canary. `git diff --check` đạt;
scratch/media/DB/log không stage, Cloud results bị Git bỏ qua.

Số dư quan sát **198,56 → 194,36 CU**, giảm **4,20 CU / trần 5 CU** cho
toàn đợt, kể cả failed/cancelled. Ledger ghi riêng **4,3137 CU ước tính
runtime**, không gọi là hóa đơn thực. Audit cuối xác nhận không session,
assignment, active job hoặc pending cleanup; ba nguồn không đổi hash.

**Không còn P0/P1 đã xác minh trong phạm vi ba mẫu nghiệm thu.** Có thể
chuyển sang việc kiểm soát phát hành/khôi phục của Giai đoạn 8, giữ Cloud
mặc định tắt. Kết quả này không chứng nhận độ chính xác chung cho mọi video.
