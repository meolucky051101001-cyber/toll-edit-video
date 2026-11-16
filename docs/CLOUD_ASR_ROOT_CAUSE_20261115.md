# Điều tra và sửa ASR Cloud — 15/11/2026

Checkout: `C:\tool_v1_clean\cloud-phase0-1-20261114`. Chỉ sửa Cloud; mặc định
`V1_CLOUD_MODE=False`. Các lỗi đã xác minh đã khép bằng Canary mới;
đủ điều kiện bàn giao sang triển khai Giai đoạn 8, chưa bật phát hành.

## Kết quả hiện tại

**Chốt 16/11/2026:** job `cloud-20261115-232507-db8a2a`, session
`cjob07db8a2aa1`, hoàn tất ba mẫu với **59/59 CapCut TTS**, không fallback.
Các lỗi sai nghĩa cắt/chất liệu, giá thêm “nghìn”, mất prefix và ASS kết
thúc sớm đã được sửa và kiểm chứng trên thành phẩm mới. Đối chiếu đạt
**6/6**, max drift ranh giới nhóm **0,201 s**. Người dùng xác nhận bốn
đoạn thành phẩm nghe rõ và giữ cách đọc; đoạn 30,4 s không có lời riêng.
Xem mục 7 của [báo cáo nội dung/timing](CLOUD_CONTENT_TIMING_REVIEW_20261115.md).

Các phần tiếp theo giữ lịch sử điều tra và số liệu của job cũ; mục cuối
ghi nhận bản sửa sau đối chiếu. Không dùng FAIL hay chi phí snapshot cũ
làm trạng thái hiện tại.

Job `cloud-20261115-122853-212130`, phiên T4 `cjob53212130a1`, hoàn tất cả ba
video qua ASR, OCR, dịch, CapCut TTS và render với bản sửa hiện tại. Các lỗi
đã tái hiện gồm timestamp vượt nguồn 59,98 s, vòng lặp ở biên frame, bỏ mất
phần lời sau giây 30 và câu giả cuối video không còn xuất hiện trong lần này.
Checksum, giải mã toàn bộ video và các cổng ASR đều đạt; cả ba mẫu định vị
được hộp OCR cho toàn bộ cue. Hồi quy: **207 passed, 1 skipped**.

**Chưa nghiệm thu đủ sáu tiêu chí và chưa mở Giai đoạn 8.** Bộ đối chiếu
Local–Cloud còn báo FAIL về số cue và timing theo ID do hai bản chia câu
khác nhau; đối chiếu bổ sung ở trên phân biệt lỗi ghép ID với lỗi thực trên
thành phẩm. Xác nhận nghe chỉ khép nghi vấn 30,4 s, không chứng nhận mọi chữ.

Đối soát toàn đợt: số dư giảm **3,26 CU** trong trần 5 CU; không còn session,
assignment hoặc cleanup chờ xử lý. Đây là chênh lệch số dư quan sát, khác
với tổng ledger **ước tính runtime 3,7044 CU**. Chi tiết kiểm chứng cuối báo cáo.

## Nguyên nhân đã tái hiện trên T4

Video Unicode dài 32,633333 s; vocal dài khoảng 32,647 s. Whisper nạp 522.356
mẫu ở 16 kHz, tương đương 32,64725 s. Không phải lỗi đổi sample rate hoặc tách
âm làm kéo dài nguồn thành 60 giây.

Whisper `20240930` chia input thành cửa sổ 30 giây. Cửa sổ cuối bắt đầu ở
30 giây, chỉ còn khoảng 2,64 giây thật và được đệm lên 30 giây. Decoder sinh
câu `请不吝点赞 订阅 转发 打赏支持明镜与点点栏目` với timestamp token cuối
`51864`, tương ứng 29,98 giây trong cửa sổ: **30 + 29,98 = 59,98 giây**.
Trong `whisper/transcribe.py`, nhánh không có cặp timestamp liên tiếp thay
`segment_duration` bằng timestamp token cuối mà không giới hạn theo số frame thật.

Đoạn này không vượt bộ lọc no-speech mặc định: `no_speech_prob=0.3724`, thấp
hơn 0,6; `avg_logprob=-0.0884` cũng biểu thị độ tự tin cao. Đây là lỗi sinh lời
ảo trên đoạn không có lời, kèm mốc decoder trỏ vào phần đệm.

Thí nghiệm cùng model, cùng vocal, seed 0:

| Cấu hình | Số cue | Mốc cuối | Kết quả |
|---|---:|---:|---|
| Decoder mặc định | 17 | 59,98 s | Tái hiện lỗi, có câu ảo |
| `word_timestamps=True` | 17 | 32,28 s | Mốc trong nguồn nhưng vẫn còn câu ảo |
| Thêm `condition_on_previous_text=False` | 17 | 32,28 s | Vẫn còn câu ảo |
| Thêm `hallucination_silence_threshold=1.0` | 17 | 32,28 s | Vẫn còn câu ảo |
| Cả hai tùy chọn trên | 17 | 32,28 s | Vẫn còn câu ảo |

Căn chỉnh từ của câu ảo có đa số từ bị dồn về cùng thời điểm, thời lượng bằng 0.
Bản sửa chỉ bật word alignment đã bị loại; job trung gian
`cloud-20261115-005504-345a24` được hủy và xác minh thu hồi phiên GPU.

Bằng chứng gốc nằm trong thư mục Git-ignored
`scratch/canary_run_20261114/asr_fix/cjob04345a24a1/`:
`asr_diagnostic.json`, `asr_tail_variants.json`, `whisper_transcribe_source.txt`
và log thí nghiệm. Mã provider/ledger chung vẫn lưu tại `canary_jobs.db`.

## Bản sửa

- Cloud dùng `large-v3-turbo`, cùng dòng model mặc định của Local V1, qua
  `openai-whisper==20240930`. Manifest, lệnh nạp model và audit ASR cùng ghi
  lựa chọn này. Local sử dụng faster-whisper nên không khẳng định hai runtime
  sinh cue hoặc nội dung hoàn toàn giống nhau.
- Pin VAD Silero ONNX đi kèm `faster-whisper==1.2.1`, cùng bộ dò của Local;
  `ctranslate2==4.8.4` và `onnxruntime==1.20.1` trong requirements/manifest.
  Whisper vẫn giải mã bằng runtime `openai-whisper==20240930`.
- Xác định vùng lời trên cả audio gốc và vocal ở 16 kHz; hợp các vùng phát hiện,
  thêm đệm 400 ms, giữ các khoảng nghỉ ngắn bằng gộp gap tối đa 1 giây.
  VAD threshold 0,4 và minimum silence 600 ms giống chính sách Local V1;
  dùng minimum speech mặc định 0 để không tự loại câu ngắn.
- Whisper chỉ giải mã các vùng này bằng `clip_timestamps` theo thời gian tuyệt
  đối, bật căn chỉnh từng từ và không truyền nội dung từ cửa sổ trước.
  Không nối audio rồi làm mất offset; không lọc bằng danh sách câu cấm.
- Chặn nếu cue ra ngoài vùng lời, vượt thời lượng nguồn, hoặc có đa số từ dài
  0 giây trong chuỗi từ đủ dài. Không tự cắt mốc hay xóa cue sau nhận dạng để qua QC.
- Ghi vùng lời từng track, vùng hợp, cấu hình và raw segments/word alignment
  vào audit ASR; metadata thành phẩm chứa audit này để đối chiếu.
- Chuẩn hóa biên `clip_timestamps` theo số frame mel thật của Whisper (hop
  160 mẫu ở 16 kHz). Mẫu dài có 957.428 mẫu: mel chỉ có 5.983 frame nhưng
  `round(59.83925 * 100)=5984`. Nếu gửi biên 5.984, vòng lặp upstream có
  `segment_size=0`, `seek` không tăng và không thoát. Biên decoder cuối nay là
  59,83 s; không cắt file audio hay sửa timestamp câu sau nhận dạng. Phần dưới
  10 ms ngoài frame đầy đủ vốn không có trong miền frame mà Whisper xử lý.
- Ưu tiên nhận dạng audio gốc như Local V1; Demucs vẫn tách nhạc nền cho mixer.
  Kiểm tra vùng VAD gốc bị ASR bỏ trống hơn 3 giây. Khi thiếu lời, độ tin cậy
  thấp hoặc căn chỉnh không hợp lệ, thử lại trên vocal đúng một lần với cùng
  model và chính sách kiểm tra; lưu cả kết quả bị từ chối. Nếu vẫn lỗi, chặn bằng
  `ASR_TIMING_INVALID`. Đây là ngưỡng chặn bảo thủ, có thể cần kiểm tra thủ công
  với nhạc/tiếng động mà VAD nhận nhầm; không hạ QC để cho qua.

VAD cục bộ trên audio gốc video lỗi tìm thấy lời đến 28,04 s, không phát hiện
lời ở đoạn 30–32,6 s. VAD không phải bằng chứng tuyệt đối về đầy đủ lời cho
mọi video; kết quả GPU trên ba mẫu và giới hạn nghiệm thu được ghi ở cuối báo cáo.

## Kiểm chứng trước khi đổi model

- 18 bộ hồi quy Cloud + Local tại mốc trước khi đổi model: **202 passed, 1 skipped,
  3 warnings (20,56 s)**.
  Skip quarantine có sẵn; warnings deprecation và coroutine mock Telegram có sẵn.
- Test bổ sung: mốc căn chỉnh hợp lệ/không hợp lệ, vùng lời chỉ thấy trên audio
  gốc, hợp vùng và giữ offset, căn chỉnh từ dồn 0 giây, không giải mã khi VAD
  không phát hiện lời.
- Ca `cloud-20261115-012140-e49b1b` xác minh được Unicode (16 cue, cuối 27,63 s,
  giữ câu thật `妥妥学生党好物`, hết câu ảo) và video ngắn (17 cue, cuối 26,18 s).
  Mẫu dài gặp lỗi biên frame nói trên; timeout 900 s, cleanup verified. Không
  coi ca này là hoàn thành. Báo cáo từng mẫu đã tải về trước khi thu hồi phiên.
- Test biên clip 5.983/5.984 đã thêm; bộ quality/export **15 passed** sau sửa.
- Job `cloud-20261115-014232-4b83db` bị dừng khi runtime chưa tạo, không còn
  tiến trình setup và kernel trước đó ở trạng thái defunct; cleanup verified.
- Job `cloud-20261115-015201-9a0e1f` hoàn tất toàn pipeline Unicode:
  16 cue dịch và CapCut TTS, video 720×1280 dài 32,633333 s, QC kỹ thuật đạt,
  cleanup verified. Lỗi ASR 59,98 s và câu ảo cuối không tái diễn.
  OCR vẫn 0/16 hộp tìm được; không coi đây là nghiệm thu phụ đề hình ảnh.
- Mẫu dài trong lần này đã thoát vòng lặp nhưng chỉ nhận dạng đến 31,02 s.
  Đối chứng Local trên audio gốc đoạn 30–60 s nhận được lời thật đến 59,42 s,
  khớp phụ đề thấy ở giây 50. Vì vậy chưa thể chứng nhận đủ lời cho mẫu dài.
  Đã thêm retry audio gốc và chặn bỏ trống vùng lời; **17 quality/export tests
  passed** tại mốc này. Kết quả xác minh lại cùng model trên T4 ở phần kế tiếp.

### Kết quả trung gian sau khi thêm retry audio gốc

Ca `cloud-20261115-025025-5616bc` cho thấy Unicode và video ngắn vẫn có mốc hợp lệ; mẫu dài được retry trên audio gốc nhưng vẫn bỏ trống các vùng lời `30,424–44,232`, `45,464–53,992` và `54,136–57,660 s`. Pipeline trả `ASR_TIMING_INVALID` và không dịch/TTS/render mẫu dài. Đây là fail-closed đúng chính sách, không phải Cloud PASS.

Đối soát cuối sau ca: số dư **196,58 CU**, giảm **1,98 CU** so với 198,56 CU ban đầu; ledger ước tính runtime **2,1888 CU**; `sessions=[]`, `active_assignments=0`, không pending cleanup, SHA256 ba đầu vào không đổi.

Ở mốc kiểm tra này, timestamp đệm 59,98 s và câu ảo cuối đã hết trên T4:
Unicode còn 16 cue, kết thúc khoảng 27,5 s; video ngắn 17 cue, kết thúc
26,18 s. Mẫu dài vẫn thiếu lời sau retry audio gốc. Kết quả sửa tiếp nằm dưới.

## Mất lời sau giây 30: đối chứng model, track và biên cửa sổ

Ca `cloud-20261115-104257-42f7e0`, phiên `cjob5742f7e0a1`, đã cài môi trường
T4 thành công nhưng kết nối trả stdout của CLI setup bị mất. Đã xác minh
`env_manifest_result.json` và chạy chẩn đoán trong chính phiên có ledger này.
Job chính kết thúc `ENV_NETWORK_ERROR`, cleanup verified; không ghi là một
job full pipeline thành công.

Trên cùng phiên, `large-v3` với audio gốc và cửa sổ `[29.0, 59.83]` nhận
20 cue đến 58,18 s. Cùng cửa sổ nhưng dùng vocal chỉ có hai câu sai.
Cửa sổ bắt đầu ở 30,424 s hoặc retry cả video theo các biên VAD lại bỏ mất
nhiều lời. Thêm 1,5 s ngữ cảnh trước mỗi vùng vẫn thất bại. Vì vậy không
quy toàn bộ nguyên nhân cho Demucs: chất lượng track và cách decoder xử lý
cửa sổ đều ảnh hưởng; không thêm mốc 29 giây đặc thù mẫu vào production.

Đổi duy nhất model sang `turbo` (alias của `large-v3-turbo`) với cùng vocal,
VAD và chính sách căn chỉnh: **27 cue, lời đến 59,26 s, không còn vùng VAD
gốc bị bỏ trống quá 3 s**. Nhận dạng có các câu ở 31–59 s mà large-v3 bỏ mất.
Không cần retry audio gốc trong phép thử này. Bằng chứng:
`scratch/canary_run_20261114/asr_fix/cjob5742f7e0a1/long_window_diagnostic.json`,
`long_fixed_asr.json`, `long_fixed.srt` và `context_retry.log`.

Bản sửa dùng model này và giữ retry có giới hạn cùng các kiểm tra chặn thiếu
lời. Đã bỏ thử nghiệm đệm biên 1,5 s do không có bằng chứng nó sửa lỗi.
VAD/căn chỉnh chỉ xác minh cấu trúc thời gian, không chứng minh mọi chữ đều đúng.

## OCR loại nhầm toàn bộ kết quả đầu vào

Cloud tạo các block EasyOCR thô với `in_subtitle_band=False`, trong khi
`select_chinese_subtitle_band` loại ngay mọi block mang cờ này. Kết quả là
0/N hộp dù EasyOCR có phát hiện chữ, và dùng mặc định vùng dưới 0,78.
Đã bỏ cờ trước bước phân loại. Kiểm thử tích hợp dùng selector thật xác nhận
các phụ đề ở vùng trên được nhận đúng. Việc sửa này không thay ngưỡng OCR.

## Hồi quy của bản sửa model và OCR

18 bộ Cloud + Local sau bản sửa cuối, gồm model, track, decoder, VAD và OCR:
**207 passed, 1 skipped, 3 warnings (31,63 s)**.
Skip quarantine và ba cảnh báo đã có trước. Test mới xác nhận model thực sự
được nạp khớp manifest/audit và kiểm tra OCR đầu vào chưa phân loại; các ca
mốc vượt nguồn, vòng lặp biên frame, retry thất bại và lời dồn 0 giây vẫn đạt.

## Câu cuối không đáng tin sau temperature fallback

Lần full pipeline `cloud-20261115-110810-a576d0` hoàn tất ba mẫu trong cùng
phiên T4: ngắn 12 cue, dài 26 cue, Unicode 15 cue. Checksum và giải mã video
đều đạt. OCR định vị lần lượt 12/12, 25/26 và 15/15 hộp. Video Unicode dùng
đúng vùng trên `y=0,1898`, không còn mặc định vùng dưới. Tuy nhiên QC kỹ thuật
không chứng minh toàn bộ lời nhận dạng đúng.

Kiểm tra raw audit phát hiện câu cuối mẫu dài, 58,26–59,70 s, là `ohl pod了`.
Whisper đã chạy hết dãy temperature mặc định tới **1,0**, rồi trả kết quả dù
`avg_logprob=-4,4116`; xác suất từng từ khoảng 0,000032–0,00879. Bản dịch
`Đỉnh của chóp luôn` được sinh từ câu sai này. Đầu ra trung gian này không
được dùng để chứng nhận ASR hoàn tất.

Bản sửa bổ sung dùng **beam_size=5, temperature=0** trên cả hai track; không
nâng temperature để chọn một câu ngẫu nhiên khi decoder không đạt.
Audit ghi rõ chính sách. Kết quả có `avg_logprob < -1,0` (ngưỡng decoder
mặc định của Whisper) hoặc không hữu hạn phải retry một lần; nếu vẫn không đạt thì dừng bằng
`ASR_TIMING_INVALID` trước SRT/dịch/TTS. Không bỏ hoặc sửa chữ của cue để qua
QC. Đây là ngưỡng chặn độ tin cậy cực thấp, không phải chứng nhận chính xác
ngữ nghĩa cho các câu còn lại.

Ngưỡng trung gian -2,0 không đủ: trong lần chạy lại, vocal sinh `谢谢大家`
dài chỉ 40 ms với điểm -1,2414. Cả beam search và greedy cố định trên audio
gốc ở phép đối chứng đều không sinh câu này, giữ câu ngắn ở 30,42–31,14 s
mà vocal beam search bỏ mất, và giữ lời tới câu `你学废了吗` ở 58,24 s.
Do đó ưu tiên audio gốc; vocal chỉ là phương án retry. Không ghép riêng câu
của các bản nhận dạng hoặc hard-code từ/ngôn ngữ để làm đẹp kết quả.

Ca `cloud-20261115-113510-c87fee` là phiên chẩn đoán, kết thúc `EXEC_FAILED`
theo thiết kế để thu hồi và không giả làm job full pipeline. Kết quả phép
thử đã lưu tại `asr_fix/cjob10c87feea1/turbo_decode_diagnostic.json`. Bản thử
vocal ưu tiên còn lỗi ở Unicode nên bị loại. Các test tái hiện cả điểm
-1,2414 lẫn lỗi alignment để xác nhận retry và chặn khi cả hai track vẫn lỗi.

## Bộ dò lời tạo vùng giả ở nhạc cuối

Ca `cloud-20261115-120216-99e6f8` xác nhận chỉ đổi model/decoder/track chưa
đủ: Silero JIT 6.2.0 tạo thêm vùng gốc `59,128–59,839 s`. Whisper giải mã
vùng này thành `放入冰箱` hoặc `好` với điểm dưới -1; cổng kiểm tra chặn
đúng, nhưng chưa giải quyết việc video có lời hợp lệ bị từ chối.

Đối chứng cục bộ trên cả ba file nguyên vẹn bằng bộ dò ONNX đi kèm
faster-whisper 1.2.1 và chính sách Local (`threshold=0,4`, silence 600 ms,
pad 400 ms) kết thúc vùng lời mẫu dài tại **58,768 s**, không có vùng cuối
59,128 s. Dùng cùng tham số với JIT 6.2.0 vẫn tạo vùng giả, nên vấn đề
không chỉ là ngưỡng. Không thay ngưỡng theo riêng video; Cloud dùng đúng bộ
dò và chính sách Local đã kiểm chứng. Vẫn lấy hợp vùng vocal/gốc để giữ lời
mà tách âm có thể làm mất. Bằng chứng: `asr_fix/local_vad_policy.json` và
`asr_fix/silero_local_policy.json`.

## Kiểm chứng cuối trên ba video thật

Job `cloud-20261115-122853-212130` có trạng thái `completed`, cleanup
`verified`. Cả ba video chạy tuần tự trong cùng phiên T4 có ledger
`cjob53212130a1`; dùng `large-v3-turbo` qua `openai-whisper==20240930`,
VAD ONNX của faster-whisper 1.2.1 và chính sách decoder nêu trên.

| Mẫu | Thời lượng nguồn | Thời lượng đầu ra | Cue / TTS đã xác minh | Mốc ASR cuối | Hộp OCR |
|---|---:|---:|---:|---:|---:|
| Ngắn | 27,212336 s | 27,200000 s | 14 / 14 | 26,20 s | 14 / 14 |
| Dài | 59,833333 s | 59,833333 s | 20 / 20 | 58,24 s | 20 / 20 |
| Tên Unicode | 32,633333 s | 32,633333 s | 15 / 15 | 27,52 s | 15 / 15 |

Cả ba mẫu nhận dạng từ audio gốc, không cần retry vocal. Audit không có
vùng lời bị bỏ trống quá 3 giây, lỗi căn chỉnh hoặc cue dưới ngưỡng độ tin
cậy; `avg_logprob` thấp nhất lần lượt khoảng -0,1735, -0,2519 và -0,1579.
Số bản dịch và tệp CapCut TTS đã xác minh bằng số cue ASR; SRT tiếng Việt
không sót CJK. SHA256/kích thước thành phẩm tải về khớp metadata từ GPU và
FFmpeg giải mã toàn bộ cả ba video thành công. Ba SHA256 đầu vào không đổi.

Đã xem ảnh đối chiếu nguồn/đầu ra: vùng che chữ nằm tại vị trí phụ đề nguồn,
mẫu Unicode dùng vùng trên `y=0,1805`; mẫu ngắn và dài lần lượt `y=0,7609`
và `y=0,7333`. Mẫu dài có lời dịch/phụ đề tại giây 50 và 57; không còn bị
cắt nội dung tại khoảng giây 31 như các lần lỗi trước.

Bằng chứng Git-ignored trong `scratch/canary_run_20261114/`:

- `asr_fix/cjob53212130a1/{short,long,unicode}/`: `output_video.mp4`, SRT
  nguồn/tiếng Việt, ASS, metadata, audit ASR và log của từng mẫu.
- `asr_fix/cjob53212130a1/review.json`: kiểm tra từng video; `parity.json`
  và `parity.md`: kết quả bộ đối chiếu chính thức.
- `asr_fix/cjob53212130a1/visual_review/`: ảnh đối chiếu ba video.
- `results/cloud-20261115-122853-212130/attempt_1/`: thành phẩm Unicode do
  orchestrator tải về; bản sao cũng nằm trong thư mục mẫu ở trên.
- `final_audit.json`: số dư, ledger, trạng thái provider/cleanup và hash nguồn
  sau lần chạy cuối; tệp này đã được cập nhật, không còn là snapshot đợt đầu.

## Giới hạn nội dung và cổng Giai đoạn 8

Phần dưới ghi nhận giới hạn tại lần kiểm tra GPU cuối. Đối chiếu tiếp theo
đã hoàn tất trong [báo cáo nội dung/timing](CLOUD_CONTENT_TIMING_REVIEW_20261115.md):
không còn treo việc nghe đoạn 30,4 s, nhưng có lỗi nội dung và ASS thực cần sửa.

Local đối chứng video ngắn có 8 cue, Cloud có 14 cue. Bộ so sánh hiện tại
ghép theo ID/số cue nên `cue_completeness` FAIL và `timing_parity` FAIL
(max drift theo ID 9,152 s). Vì cách chia câu khác nhau, số drift này chưa
chứng minh tiếng/phụ đề thực tế lệch hình 9,152 s, cũng chưa chứng minh
Cloud thừa sáu câu về nội dung. Cần căn theo nội dung và thời gian trước
khi kết luận hai tiêu chí này; giữ nguyên kết quả FAIL của bộ so sánh.

Bốn tiêu chí còn lại đạt theo bộ so sánh: vai giọng, tính hợp lệ phụ đề,
chất lượng video và chi phí/thu hồi. Vai giọng chỉ được đối chiếu trên mẫu
một giọng; kết quả này không chứng nhận dual-voice hoặc mọi nội dung ASR.

Caption khoảng 30,4 s ở video dài có thể là `都是齐的`; bản Cloud và phép
đối chứng trực tiếp bằng Local Faster-Whisper đều không có cue riêng ở đó.
Thử mở rộng ngữ cảnh nhận `都是奇的` nhưng đồng thời tái sinh câu cuối sai,
nên không đưa cấu hình đó vào production. Tại mốc GPU này chưa nghe đối chiếu;
không coi caption là transcript lời nói đã xác nhận và không khẳng định
ASR đầy đủ từng chữ. Bằng chứng trực tiếp:
`asr_fix/cjob53212130a1/faster_runtime_comparison.json` và ghi chú trong
`review.json`. Chưa có bằng chứng chuyển toàn bộ decoder sang Faster-Whisper
sẽ xử lý được điểm còn nghi vấn này.

Sau đó người dùng nghe clip nguồn 28,5–34 s và xác nhận “Không có, chỉ
nhạc/tiếng động” trước `完美`; không tính đoạn 30,4 s là thiếu lời. Kết quả
nghe và các lỗi nội dung/timing thực được ghi trong báo cáo bổ sung ở trên.

## Chi phí và thu hồi cuối đợt

Số dư đầu 198,56 CU, số dư cuối quan sát 195,30 CU: giảm **3,26 CU** cho
toàn bộ đợt Canary và điều tra, dưới trần 5 CU. Ledger lưu tổng **3,7044 CU
ước tính theo runtime**; riêng job cuối ước tính 0,27 CU. Không gắn nhãn các
ước tính này là hóa đơn thực tế; chênh lệch số dư cũng có thể cập nhật trễ.

Provider xác nhận `sessions=[]`, `active_assignments=0`, `usage_rate=0`;
không active job hoặc pending cleanup, toàn bộ ledger đã đối soát và
`cleanup_verified=1`. Cloud mặc định vẫn tắt. Bản sửa đã qua 18 bộ hồi quy
**207 passed, 1 skipped, 3 warnings (31,63 s)**; skip quarantine và warnings
có sẵn. Giai đoạn 8 tiếp tục đóng cho tới khi hoàn tất nghiệm thu nội dung
và timing theo các câu tương ứng.

## Bản sửa sau đối chiếu nội dung/timing — chốt 16/11/2026

Nguyên nhân còn lại thuộc nhiều tầng, đã được sửa bằng cơ chế chung:

1. Cue ASR dài gộp nhiều câu khiến dịch/TTS dồn đầu: chia theo mốc từ,
   khoảng nghỉ/mệnh đề, giữ nguyên text và lineage.
2. ASS lấy end theo TTS ngắn hơn nguồn: tách timeline chữ và source mask,
   kiểm tra khoảng phủ/text bằng ASS đã phát ra, không chỉ đếm hộp OCR.
3. Đồng âm ASR và caption trễ làm sai chữ/mất prefix: kiểm duyệt sửa chữ
   với nhiều frame, bảo toàn phần chưa có bằng chứng và từ/số/Latin.
4. Dịch tự bổ sung giá/đơn vị: guard giá không có nguồn và rà bản dịch cuối.
5. MP3 thêm padding sau atempo làm vượt cửa sổ cue: fit sang WAV PCM,
   dự phòng biên và kiểm tra duration/tốc độ sau mã hóa; vẫn CapCut TTS.
6. Review/rephrase API gặp quota hoặc model không khả dụng: retry/fallback
   model có giới hạn, lỗi kiểm duyệt phải hiện rõ và không tự báo PASS.

Video dài hiện 30 câu, ngắn 14 và Unicode 15. Ba mẫu đạt full decode,
metadata hash, QC tải về, ASS coverage và TTS boundary audit. 11 file code
khớp receipt trước lần Canary cuối. Hồi quy 324 passed/1 skipped, sau đó
mock Telegram kiểm tra riêng 1 passed với RuntimeWarning thành lỗi.

`scratch/content_fix_20261115/human_listening_final.json` giữ nguyên trả
lời “Nghe rõ, giữ cách đọc hiện tại” cho bốn clip; không đổi pronunciation
sau xác nhận. Nội dung đã rà độc lập theo caption, timing nhóm theo Local,
ASS/audio theo thành phẩm; không nhận đây là nghe hết từng từ trên mọi video.

Chi phí cập nhật toàn đợt: **4,20 CU** chênh lệch số dư / trần 5 CU;
**4,3137 CU** là tổng ước tính runtime riêng. Audit cuối không còn session,
assignment, active job hoặc cleanup treo. Ba hash nguồn không đổi.
**Đã khép các lỗi chặn nghiệm thu đã xác minh; sẵn sàng triển khai Giai đoạn 8.**
