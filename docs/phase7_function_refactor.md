# Phase 7 — tách hàm, giữ nguyên hành vi

Nhánh `codex/phase-7-function-refactor`, bắt đầu từ `3ce63b9`. Baseline:
1136 passed / 0 failed / 79 skipped (Python 3.10 có 213 subtests passed).
Không đổi model, thứ tự pipeline, voice, mixer, style ASS, QC hoặc retry.
Không restart dịch vụ, cutover/rollback hay dùng video production để render.

## execute_job

Tách orchestration thành context riêng cho mỗi lần gọi và các method stage.
Kết quả audio/subtitle của stage truyền qua tham số/return; không lưu trạng
thái đang chạy trên instance dùng chung. Context chứa các cấu hình đã đóng
băng, đường dẫn, callback, thời lượng và thông tin checkpoint của đúng job.

- execute_job: 903 → 75 dòng. Helper dài nhất 110 dòng, đều dưới 150 dòng.
- Giữ extract → separate → ASR → OCR → translate → TTS/ASS → mix → render →
  QC/deliver. Input đã tải trước khi vào execute_job; không thêm stage download.
- Giữ vị trí check stop/pause, GPU gate, timeout, ghi start/complete/failure,
  artifact sidecar và kiểm tra version khi resume.
- Nhánh visual OCR thay ASR được tách riêng; OCR reader releaser từ nhánh đó
  vẫn khả dụng nếu việc import ở stage OCR tiếp theo thất bại.
- Đối chiếu AST của 9 block stage sau khi inline helper và quy đổi context:
  các phép tính, call, điều kiện, nhánh except/raise/return cũ khớp. Phần giữ
  binding OCR reader giữa hai scope được kiểm tra riêng.
- Ba golden contract đã có giữ fixture byte-identical. Bổ sung visual OCR
  reuse/BGM gốc và cancellation từ progress callback. Cả 5 contract pass trên
  code gốc `3ce63b9` và code refactor trong process test cô lập.
- Test AST strict OCR/dịch được cập nhật để duyệt các helper reachable từ
  execute_job; giữ nguyên assertion strict_geometry=True và strict=True.

## generate_ass_file

Tách chuẩn hóa geometry và page timing, phân trang, khóa band, tạo cover event,
format text và vẽ sticker có/không có source. Layout/band/text metrics dùng
record bất biến; giữ deepcopy segment trước khi chỉnh thời gian và geometry.
generate_ass_file: 653 → 134 dòng; helper dài nhất 130 dòng.

72 case đối chiếu với code gốc cho output ASS giống từng byte (bao gồm BOM):
portrait/landscape/square, source sát biên/giữa/không có, geometry verified,
solid/frosted, có/không watermark, văn bản dài và ký tự escape. Đồng thời kiểm
tra input không bị mutate. Golden pipeline cùng 40 test layout/cover/tracking
đạt tổng 45 passed trên Python 3.12.

## perform_video_ocr

571 → 59 dòng ở hàm public, helper dài nhất 133 dòng. Giữ nguyên hai decorator
stage/ocr_proxy trên entry point. Metadata/strategy, timestamps, frame sampling,
rescue, gán geometry và ghi report được tách riêng. `_OcrFrameBatch` giữ danh
sách frame bounded, ROI và cờ early band lock riêng cho mỗi lần gọi; không đổi
reader/model session toàn cục.

Đối chiếu AST 10 helper và flush_frames với các block gốc sau quy đổi thuộc
tính batch: phép tính, điều kiện, vòng lặp, try/finally và lời gọi cũ khớp.
Giữ metrics trong report, ghi report trước strict validation và các nhánh
subtitle-free relaxation/CJK fail-closed. Bộ OCR + golden Python 3.12:
164 passed / 9 skipped. Không sửa test OCR để đổi kỳ vọng.

## process_single_local_video

540 → 71 dòng; helper dài nhất 104 dòng. Tách nhánh orchestrator, nhánh v2 có
sẵn và các bước legacy: chuẩn bị audio/snapshot, ASR/OCR, dịch, chọn voice,
TTS/ASS/mix, render/delivery. Không bật v2/shadow hoặc thay lựa chọn runtime.
Giữ nguyên sự khác nhau giữa cancellation ở nhánh orchestrator (propagate) và
legacy (mark stopped/False), cùng finally dọn RAM/VRAM chỉ ở nhánh legacy.

Context `_BatchJob` chứa đường dẫn/callback của từng invocation; record voice
giữ nguồn, param, ID, mode và segment voice map. Không tạo state job trên global
dùng chung. Đối chiếu AST các block stage, hai nhánh delegate và ranh giới
except/finally với code gốc đều khớp sau quy đổi tên context/record.

Bốn contract qua public batch entrypoint được chạy trước và sau refactor:
success ghi receipt và không finish tracker hai lần; lỗi orchestrator giữ
checkpoint, không chạy legacy; cancellation propagate; nhánh legacy giữ đúng
stage order, segment_voices và byte thành phẩm giao tới output tạm. Tất cả
engine nặng được mock. Test cấm ghi đè QC thành WARN duyệt cả helper reachable,
giữ nguyên hai assertion gốc. Bộ batch + golden + QC Python 3.12: 12 passed.

Fixture `tests/fixtures/phase7/execute_job.json` giữ nguyên SHA256:
`ee4e5a27365ebb2cded35198a545b1661248026ac1529729dda2ef283eb9bb57`.

## Nghiệm thu từng bước

| Hàm vừa tách | Full pytest Python 3.10 | Golden Python 3.12 |
| --- | --- | --- |
| execute_job | 1138 passed / 0 failed / 79 skipped / 213 subtests; 126,93 giây | 5 passed; đối chứng code gốc cũng 5 passed |
| generate_ass_file | 1138 passed / 0 failed / 79 skipped / 213 subtests; 128,38 giây | 5 passed trong bộ tập trung 45 test |
| perform_video_ocr | 1138 passed / 0 failed / 79 skipped / 213 subtests; 142,62 giây | 5 passed trong bộ OCR 164 test |
| process_single_local_video | Xem nghiệm thu bản cuối dưới đây | 5 golden + 4 batch contract và 3 test QC: 12 passed |

Kết quả cuối hai môi trường được ghi sau khi full suite hoàn tất. Tăng tổng 6
test so với baseline: 2 contract orchestrator và 4 contract batch; không thêm
skip hoặc bỏ assertion để đạt kết quả.
Log/bằng chứng tạm nằm dưới `scratch/phase7`, không commit dữ liệu runtime.

Sau khi workspace chính chuyển về nhánh hardening và xuất hiện sửa đổi
workflow/timeline, phần còn lại tiếp tục tại worktree `C:\tool_v1_updates` trên
cùng nhánh Phase 7. Không reset/stash hoặc ghi đè các sửa đổi ở workspace chính.
Để tái hiện test vốn phụ thuộc dữ liệu local, worktree có junction tới trọng số
SenseVoice, hai model venv và Node sidecar dependency có sẵn, cùng bản sao bốn sample giọng test. Không
cài/sửa package hoặc thay đổi model venv gốc; các tài nguyên này được ignore.
Runner test bằng file thật được dùng để Windows multiprocessing spawn hoạt
động; không dùng stdin làm main. Các nhóm bị lỗi do thiếu tài nguyên/runner đã
chạy lại đạt 39 passed / 2 skipped, gồm 5 golden và 4 batch contract trên hàm gốc.

## Nghiệm thu bản cuối

| Kiểm tra | Kết quả |
| --- | --- |
| Full suite Python 3.10 | 1142 passed / 0 failed / 79 skipped / 213 subtests passed; 3 warnings; 178,73 giây |
| Full suite Python 3.12 | 1142 passed / 0 failed / 79 skipped; 6 warnings; 141,39 giây |
| Public API | Chữ ký, defaults, annotation và decorator của cả 4 hàm giữ nguyên; các hàm khác trong 4 module khớp AST baseline |
| Golden/ASS | Fixture execute_job giữ hash; 72 case ASS byte-identical và không mutate input |
| Preflight Python 3.12 | ready=true; 25/25 pass, workspace/output tạm dưới scratch |
| Dịch vụ hiện hữu, kiểm tra chỉ đọc | HTTP 200; PID cổng 8088=18000, 8090=10932; không thực hiện restart |

Log bản cuối tại `C:\tool_v1_updates\scratch\phase7`: `final-py310-verified.log`,
`final-py312.log`, `preflight.json`; các log trước khi tách worktree còn tại
`C:\tool v1\scratch\phase7`. Một lượt worktree ban đầu thiếu dependency Satori
đạt 1141 passed / 80 skipped; sau khi bổ sung alias dependency có sẵn, riêng
Satori đạt 5 passed và full suite được xác nhận lại để giữ đúng 79 skip baseline.
Không cài mới Node/Python dependency hoặc sửa model/runtime gốc.

Preflight chưa xác nhận quyền ghi output production hoặc chất lượng render
thật. Code chỉ nằm trên nhánh Phase 7, chưa merge vào tool-v1/chưa push; workspace
chính có sửa đổi song song được giữ nguyên kể từ khi tách worktree. Chưa nạp
nhánh này vào dịch vụ đang chạy hoặc chuyển sang Phase 8.

## Nghiệm thu video thật

Sau khi refactor hoàn tất, người dùng cần tự bấm chạy một video dưới 30 giây
trên dashboard để đối chiếu voice, timing, che phụ đề, BGM và QC với bản trước.
Golden và stage mock không chứng minh chất lượng audio/video thật. Lượt này
không tự restart dịch vụ để nạp code mới hoặc chọn video trong thư mục đầu vào.
