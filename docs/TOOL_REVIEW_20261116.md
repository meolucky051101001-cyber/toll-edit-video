# Review Tool V1 — 16/11/2026

Kết luận: **chưa đủ điều kiện nghiệm thu ổn định hoặc đóng gói bản hiện tại**. Runtime đang hoạt động và phần lớn test đạt, nhưng còn lỗi đảo nghĩa thoại, quản lý tác vụ/GPU, hình học phụ đề và nhánh Cloud cũ. Báo cáo này là review; không sửa mã sản phẩm, không triển khai hay chạy lại job thật.

## Phạm vi và trạng thái checkout

Checkout chính: `C:\tool v1`, branch `tool-v1`, HEAD `4b2d234acc92fead3fc097e570de523089290aa4`. Tại lúc review có 13 file tracked đã sửa, 920 dòng thêm/119 dòng xóa; còn module caption evidence, Cloud cũ và test/tài liệu chưa được Git theo dõi. Đã đọc status/diff và giữ nguyên các chỉnh sửa đó. `workspace` là junction tới `D:\workspace`, chứa dữ liệu chạy thật. Conftest tạo workspace tạm nhưng import `main` đã ghi đè biến đường dẫn đó: không thể coi lượt full suite là được cách ly hoàn toàn; xem R15.

Worktree `C:\Users\admin\.codex\worktrees\dashboard-stability\tool v1` cùng HEAD nhưng có 54 file tracked đã sửa, 28 mục untracked, 4.356 dòng thêm/2.857 dòng xóa. Hai checkout **không phải cùng một bản phần mềm**. Đặc biệt, worktree đã có sửa drain native task; finding R2 dưới đây thuộc checkout chính.

Đã rà các luồng orchestrator/checkpoint/stop/retry, Studio/API, ASR/OCR/dịch/TTS/mix/QC, Telegram/Cloud cũ, đóng gói và CI; chạy toàn bộ Python test, Node test, kiểm tra cú pháp và health chỉ đọc. Không coi review mã và test offline là bằng chứng hoàn tất render thực, nghe lại toàn bộ thoại hoặc chạy bền 24 giờ.

## Kết quả xác minh

| Kiểm tra | Kết quả | Bằng chứng/giới hạn |
|---|---|---|
| Python full suite trên Python 3.12.14 | **1.360 passed, 8 failed, 79 skipped**, 124,99 giây | [Log](<C:/tool v1/scratch/full_review_20261116/full-suite.log>), [JUnit](<C:/tool v1/scratch/full_review_20261116/full-suite.xml>) |
| 3 test shutdown thất bại trong full suite, chạy riêng | **3 passed** | [Log riêng](<C:/tool v1/scratch/full_review_20261116/shutdown-isolated.log>); đã chứng minh `main` đổi workspace sang dữ liệu thật, xem R15 |
| Geometry riêng | **67 passed, 5 failed, 1 skipped** | Cùng năm lỗi hình học xuất hiện trong full suite |
| Test rút gọn thoại bị quarantine, chạy riêng không skip | **1 failed** | `test_invalid_condensation_never_cached`: nhận câu mất phủ định và số lượng |
| JavaScript | **13 passed, 0 failed** | 5 file `tests/js/*.test.js`, gồm token dashboard, queue, autosave, binding và timeline |
| Parse cú pháp backend | **253 file Python đạt** | Không bao gồm venv; không chứng minh chuỗi Python sinh động trong Cloud hợp lệ |
| `pip check` | **Đạt** | Không có dependency bị vỡ trong venv chính |
| Health chỉ đọc | **Đạt** | Python đúng 3.12.14, preflight 25/25; dashboard idle; tool-control chỉ xác nhận TCP listening vì chưa có `/health` |
| Đóng gói dry-run | Chọn **581 file, 9,88 MB**, nhưng thiếu dependency mới | [Log](<C:/tool v1/scratch/full_review_20261116/package-dry-run.log>); xem R5 |
| `git diff --check` | **Đạt** | Có cảnh báo CRLF/LF, không có lỗi whitespace |

Trong 79 test bị bỏ qua, **74 test nằm trong quarantine**, 2 test Remotion cần opt-in, 3 test thiếu fixture benchmark. Test SenseVoice ONNX với fixture `zh.wav` thực có chạy và đạt. Không có test nào được bỏ qua do runner chặn mạng trong lượt full-suite hợp lệ.

Runner review chặn kết nối ngoài và các port dịch vụ thật, cho phép socket loopback tạm mà asyncio Windows cần. Lượt đầu của runner thiếu main guard cho multiprocessing và đã bị loại khỏi kết quả; đã sửa runner, xác nhận không còn tiến trình con đó rồi chạy lại toàn bộ. Các file `runner-initial-discarded.*` chỉ là chẩn đoán runner, **không phải kết quả sản phẩm**. Số liệu full-suite ở trên là lượt hoàn tất sau sửa main guard, nhưng vẫn có hạn chế cách ly môi trường ở R15. Sau khi phát hiện R15, [runner](<C:/tool v1/scratch/full_review_20261116/run_offline_suite.py>) đã được thêm kiểm tra dừng trước khi chạy test nếu collection đổi workspace; chưa chạy lại toàn bộ. Mã sản phẩm chưa được sửa.

## Findings cần sửa

### R15 — P1: Import `main` ghi đè workspace explicit, phá cách ly chạy thử

[main.py:41](<C:/tool v1/backend/main.py:41>) gán trực tiếp từng biến `.env` vào `os.environ` trước khi gọi loader dùng chung. Đây không chỉ là cấu hình mặc định: giá trị đã được caller/conftest đặt cũng bị ghi đè.

Collect-only với `test_runtime_lifespan_security.py` chứng minh `AUTODUB_WORKSPACE` chuyển từ thư mục test tạm sang `D:\workspace`. `read_telegram_telemetry` sau đó đọc `D:\workspace\control\v1.json`, có đúng PID 10932 xuất hiện trong ba failure shutdown. [Bằng chứng đã lọc chỉ còn đường dẫn và PID](<C:/tool v1/scratch/full_review_20261116/shutdown-collection-environment.json>), [log collection](<C:/tool v1/scratch/full_review_20261116/shutdown-collection.log>).

Hậu quả: môi trường chạy thử/canary hoặc caller chỉ định workspace khác có thể dùng nhầm dữ liệu thật; kết quả test phụ thuộc máy. Cần giữ ưu tiên biến được caller truyền và đưa đường dẫn/control/config stores vào fixture cách ly. Test `test_audio_settings_persistence` có ghi cấu hình thật qua default store rồi khôi phục các giá trị trong `finally`; vì không có snapshot runtime trước lượt test, không khẳng định mọi artifact runtime hoàn toàn không bị tác động. Không chạy thêm full suite trước khi khép lỗi cách ly.

Audit ảnh hưởng: cấu hình âm lượng hiện đọc được là BGM −2 dB, thoại +1 dB, RoFormer/soft/default; test không lưu backup nguyên bản để so byte. Các test ghi history trực tiếp có patch, nhưng ba đường test pipeline ở [test_pipeline_v2_video_pipeline.py:414](<C:/tool v1/tests/test_pipeline_v2_video_pipeline.py:414>), dòng 479 và 549 gọi delivery dẫn tới [video_pipeline.py:1701](<C:/tool v1/backend/pipeline_v2/video_pipeline.py:1701>) và history writer chưa được mock/restore. Chưa xác định được chính xác bản ghi nào đổi do thiếu snapshot trước test; không tự xóa hoặc hoàn nguyên những bản ghi có thể đang được job thật cập nhật. Các test notifications được rà có patch file/hàm; chưa có bằng chứng chúng ghi runtime. Dịch vụ vẫn cập nhật metadata sau suite, nên mtime riêng lẻ không chứng minh thay đổi do test.

### R1 — P1: Rút gọn để khớp TTS có thể đảo nghĩa câu

[translation.py:1907](<C:/tool v1/backend/ai/translation.py:1907>) vẫn nhận câu đã bị `validate_condensed_text` loại, chỉ vì câu ngắn hơn và không có CJK. Validator response ở dòng 1866 cũng có đường bỏ qua tương tự. Kết quả được gán trực tiếp vào `seg.content` trước TTS tại [voice_cloning.py:597](<C:/tool v1/backend/ai/voice_cloning.py:597>).

Tái hiện offline: nguồn **“Tôi không có 5 nghìn”**, provider mock trả **“Tôi có tiền”**; hàm trả `{1: 'Tôi có tiền'}` thay vì từ chối. Đây là lỗi nội dung, độc lập với sửa đồng âm/OCR ở lượt trước. Test [test_v1_speech_translation_repair.py:434](<C:/tool v1/tests/test_v1_speech_translation_repair.py:434>) hiện bị skip bởi [tests/conftest.py:102](<C:/tool v1/tests/conftest.py:102>).

Hướng sửa: mọi nhánh trả về câu rút gọn phải giữ cùng quy tắc về phủ định, số lượng và điều kiện; không dùng “ngắn hơn” để vượt kiểm định. Bật lại regression này trong cổng nghiệm thu.

### R2 — P1: Timeout nhả quyền tài nguyên khi native worker chưa kết thúc

[v1_stage_runtime.py:88](<C:/tool v1/backend/v1_stage_runtime.py:88>) cancel asyncio task rồi dùng `task.done()` để xác định đã drain. Với `asyncio.to_thread`, wrapper kết thúc nhưng thread vẫn chạy. Repro worker ngủ 1 giây, timeout 0,05 giây: `await_stage` trả lúc khoảng **0,25 giây**, `thread_finished=False`; sau đó thread mới hoàn tất.

ASR giữ GPU gate quanh lời gọi này tại [v1_orchestrator.py:652](<C:/tool v1/backend/v1_orchestrator.py:652>); gate có thể được nhả trước khi worker cũ chấm dứt. Retry hoặc job mới có thể chồng tài nguyên và ghi artifact cùng đường dẫn. Thông báo “worker stopped before retry” cũng không phản ánh trạng thái thật.

Hướng sửa: quản lý kết thúc công việc native thực, giữ quyền tài nguyên đến khi xác minh worker đã dừng; tái sử dụng và kiểm chứng sửa `drain_task` đang có trong worktree dashboard trước khi tích hợp. **Không quy lỗi này cho worktree đã sửa.**

### R3 — P1: Studio nhận dạng chỉ kiểm tra rảnh, không giữ khóa

[_wait_pipeline_free tại workflow_api.py:3957](<C:/tool v1/backend/workflow_api.py:3957>) kiểm tra lock/tracker rồi trả `True` ở dòng 3979; không acquire lock hay đăng ký quyền thực thi. [Generate CN:4011](<C:/tool v1/backend/workflow_api.py:4011>) sau đó chạy ASR trực tiếp, mỗi request mở một thread; đường này không lấy GPU gate chung như orchestrator.

Repro trên hàm thật với các phụ thuộc idle giả lập: hai worker lần lượt đều được nhận, khóa vẫn `False`. Hai request Generate CN, hoặc Studio đồng thời với Batch, có thể chạy ASR chồng GPU; cùng stem còn ghi chung `original.wav`/`original.srt`.

Hướng sửa: claim job/tài nguyên bằng thao tác nguyên tử và giữ lease đến hết tác vụ; ASR Studio phải dùng chung GPU gate và cơ chế dừng với pipeline.

### R4 — P1: `/cloud stop` báo đã dừng dù lệnh thu hồi VM sai cú pháp

[colab_bridge.py:364](<C:/tool v1/backend/cloud/colab_bridge.py:364>) và dòng 383 ghép Python một dòng dạng `...; for ...:`. Kiểm tra `compile()` trên cả hai chuỗi lấy từ AST đều gây `SyntaxError`; compound statement `for` không thể đặt sau dấu chấm phẩy theo cách này. Return code của `_run_wsl_bash` bị bỏ qua; [dòng 392](<C:/tool v1/backend/cloud/colab_bridge.py:392>) luôn trả thành công.

[telegram_bot.py:727](<C:/tool v1/backend/telegram_bot.py:727>) có đường gọi thật qua `/cloud stop`, `/cloud kill`, `/cloud cancel` và trả thông báo an toàn. Khi VM còn chạy, việc thu hồi có thể không xảy ra và CU tiếp tục bị tính. Không gọi WSL/provider để tái hiện finding này.

Hướng sửa: dùng script Python hợp lệ, kiểm tra exit code, xác minh danh sách assignment sau khi dừng rồi mới báo thành công. Finding thuộc **Cloud cũ nằm trong checkout này**, không phải kết luận về bản Cloud riêng đã nghiệm thu trước đó.

### R5 — Chặn phát hành: Gói hiện tại thiếu module caption evidence mới

[translation.py:54](<C:/tool v1/backend/ai/translation.py:54>) import bắt buộc `.v1_caption_evidence`. File này đang untracked. [package_v1_release.py:281](<C:/tool v1/package_v1_release.py:281>) lấy inventory từ `git ls-files`, do đó chọn `translation.py` nhưng không chọn module mới. Dry-run vẫn báo hoàn tất vì danh sách required chưa kiểm dependency này.

Đóng gói checkout hiện tại sẽ thiếu module khi import dịch. Đây là thiếu sót tích hợp của thay đổi đang làm dở, không phải lỗi runtime của checkout có file trên đĩa. Cần đưa module/test mới vào tập file được quản lý và kiểm import từ chính thư mục giải nén trước khi phát hành. Không tự stage/commit trong lượt review.

### R6 — P2: Vùng che vẫn xuất hiện trong đoạn đã xác nhận không có chữ

[ass_utils.py:1136](<C:/tool v1/backend/ass_utils.py:1136>) tạo `BgStyle` vô điều kiện dù có trạng thái verified. Dòng 868 còn phục hồi `best_block` cũ khi tracking rỗng.

Hai regression thất bại: chỉ được che `[0,4–1,0]` và `[1,8–2,2]` giây nhưng thực tế che liên tục cả cue `[0–3]`; verified-empty cần 0 mask nhưng vẫn có mask `[0–3]`. Hậu quả là che hình ngoài thời gian caption thực. Cần giữ phân biệt “không có bằng chứng” và “đã xác minh không có caption”.

### R7 — P2: Nhãn sản phẩm lặp bị nhận thành phụ đề

[ocr_subtitle_locator.py:825](<C:/tool v1/backend/ocr_subtitle_locator.py:825>) cứu block có `reliable=False` nếu lặp dưới 4 cue, dù kiểm tra trước đó đã loại nhãn lặp không khớp thoại. Test mong chọn cue `{1,4}`, thực tế chọn cả `{1,2,3,4}`, với cue 2/3 chứa nhãn `优质产品说明`.

Hậu quả: che nhầm bao bì/nội dung hình và đưa bằng chứng OCR không phù hợp vào các bước sau. Cần giữ quyết định loại nhãn không khớp qua bước bracket recovery.

### R8 — P2: Caption di chuyển trong cùng cue bị mất sample

[ocr_subtitle_locator.py:844](<C:/tool v1/backend/ocr_subtitle_locator.py:844>) loại sample lệch tâm thay vì giữ vị trí theo thời gian. `test_frames_same_segment_are_not_composited_across_time` cần 2 sample nhưng chỉ còn 1. Mask có thể bỏ sót vị trí chữ khi caption di chuyển; cần kiểm nghiệm tracking theo thời gian, không coi vị trí khác là nhiễu chỉ vì cùng cue.

### R9 — P2: Strict geometry nhận box suy đoán như box đã xác minh

[v1_ocr_geometry.py:174](<C:/tool v1/backend/v1_ocr_geometry.py:174>) dùng một cue có box để tự sửa các cue unresolved; dòng 190–204 đặt `x=0,25..0,75`, `prob=0,85`, `ocr_mask_status=located` dù không đo được box đó. `strict=True` với 1 cue hợp lệ và 2 cue unresolved vẫn nhận cả ba.

Regression `test_report_saved_before_unresolved_error` cũng không phát sinh `OCRGeometryError` như hợp đồng mong đợi. Hướng sửa: đánh dấu hình học suy đoán riêng, không nâng thành “đã xác minh”; quality gate phải quyết định chặn hoặc yêu cầu review dựa trên bằng chứng thực.

### R10 — P2: Giọng được truyền qua API bị thay bằng cấu hình global

[v1_orchestrator.py:198](<C:/tool v1/backend/v1_orchestrator.py:198>) dựng `voice_config` từ global, không chuyển `voice_source/voice_param` do API truyền vào thành `manual_voice`. TTS dùng cấu hình đó tại dòng 948–974.

Repro `plan_job()` offline: yêu cầu `edge/vi-VN-NamMinhNeural`, nhưng snapshot TTS vẫn là `capcut/BV562_streaming` của global. Cần quy định ưu tiên lựa chọn explicit theo job và freeze đúng cấu hình trước TTS.

### R11 — P2: Studio không tìm đúng thư mục artifact của batch

[workflow_api.py:104](<C:/tool v1/backend/workflow_api.py:104>) chỉ tìm `workspace/<stem>` hoặc stem được làm sạch. [batch_processor.py:154](<C:/tool v1/backend/batch_processor.py:154>) lưu ở `workspace/batch_<hash-path>_<stem>`.

Repro thư mục batch có `original.srt` nhưng resolver trả thư mục `<stem>` không tồn tại. Studio có thể không hiển thị sub/audio hoặc ghi chỉnh sửa sang thư mục khác. Cần map qua danh tính job/manifest thay vì suy diễn chỉ từ stem.

### R12 — P2: Hạn mức CU Cloud bỏ phí lỗi/hủy và giảm khi xóa lịch sử

[cloud_job_store.py:285](<C:/tool v1/backend/cloud/cloud_job_store.py:285>) chỉ cộng CU của job `completed`; dòng 249 xóa luôn bản ghi khi xóa lịch sử. Orchestrator chỉ ghi CU actual khi thành công.

SQLite cô lập: job lỗi có CU=3 nhưng spent=0; xóa completed CU=4 làm spent giảm 4→0. Hạn mức vì thế không bảo vệ chi phí thực. Cần sổ chi phí độc lập với lịch sử hiển thị và ghi nhận tiêu thụ trong đường kết thúc lỗi/hủy.

### R13 — P2: Trạng thái hủy Cloud bị cập nhật muộn ghi đè

[cloud_job_store.py:224](<C:/tool v1/backend/cloud/cloud_job_store.py:224>) update state vô điều kiện. Repro gọi `cancel_job` rồi cập nhật worker muộn làm `stopped` chuyển lại `downloading`; orchestrator chưa kiểm tra hủy ở các bước tương ứng.

Cần terminal-state guard và cơ chế cancel hợp tác. Router HTTP Cloud cũ liên quan **chưa được mount trong main hiện tại**; đây là lỗi hợp đồng engine/store, không khẳng định endpoint đó đang mở trên dịch vụ chính.

### R14 — P2: `/cloud <đường_dẫn_local>` vào queue rồi không được xử lý

[telegram_bot.py:807](<C:/tool v1/backend/telegram_bot.py:807>) tạo job `type=local_file`; worker tại dòng 1258 chỉ dispatch `url`, `video`, `resume_v2`, sau đó gọi `task_done()` trong finally.

File local hợp lệ được báo đã nhận nhưng không có bước xử lý. Cần có nhánh dispatch phù hợp hoặc từ chối trước khi nhận queue; bổ sung test từ nhận command tới dispatch thật.

## Chất lượng bằng chứng và nghiệm thu còn thiếu

- Năm regression hình học đã biết từ biên bản 14/11 vẫn tái hiện. Không thể dùng kết quả ASR/dịch riêng để kết luận render che chữ đúng.
- Quarantine đang che một lỗi nội dung nghiêm trọng đã tái hiện ở R1. Cần triage 74 skip theo hợp đồng sản phẩm, thay vì mặc định toàn bộ đều stale.
- Ba failure shutdown trong full suite dùng telemetry thật do lỗi ghi đè môi trường R15, nhưng cả ba pass khi chạy riêng. Không kết luận thao tác shutdown production sai chỉ từ ba assertion này; lỗi môi trường/cách ly đã được xác nhận độc lập.
- CI hiện chọn 19 file Python và bộ Node, không chạy toàn bộ 1.447 test hay đầy đủ nhóm regression mới. Một regression geometry đang nằm ngay trong tập CI được chọn.
- Năm video Local đã đối chiếu ASR/dịch ở lượt trước có candidate bản dịch, nhưng chưa tạo TTS/render mới và nghiệm thu nghe/xem đầu cuối cho 16 câu đã chỉnh. Xem [ASR_TRANSLATION_REVIEW_20261116.md](<C:/tool v1/docs/ASR_TRANSLATION_REVIEW_20261116.md>).
- Cloud cũ trong `C:\tool v1` còn được Telegram gọi; không được lấy receipt của checkout Cloud riêng để nghiệm thu nhánh cũ này.

## Worktree dashboard và hai worker

Theo [biên bản worktree](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/docs/DASHBOARD_IMPLEMENTATION_20261115.md>) và [run.json](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/test_evidence/dashboard/continuation-20261116-final/run.json>), lượt kiểm trước đạt **322 passed/7 skipped**. Đây là evidence có sẵn, không phải bộ test được chạy lại trong lượt review này.

[soak-smoke-final.json](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/test_evidence/dashboard/soak-smoke-final.json>) ghi **120,359 giây, 558 request, 4 forced exits**, không phải nghiệm thu GPU/render/Telegram thật. [worker_settings.py:37](<C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1/backend/worker_settings.py:37>) vẫn giới hạn `MAX_SUPPORTED_WORKERS=1`.

W9 còn thiếu nối registry vào dispatcher/tracker/stop/recovery và retention, nghiệm thu hai execution đầu cuối. Chưa có bằng chứng soak 24 giờ, process sống lâu để đánh giá leak, ngưỡng RAM/VRAM và throughput thực, corpus AI/GPU/Telegram, canary 24–72 giờ và diễn tập nâng dịch vụ.

Thứ tự xử lý đề nghị: khép R15 trước khi chạy thêm test diện rộng; sửa R1–R4 và lỗi geometry; thống nhất phạm vi tích hợp từ worktree; khép lỗi giọng/Studio/Cloud queue và đóng gói; gỡ quarantine liên quan; sau đó mới nghiệm thu video thật, hai worker và chạy bền theo thời lượng đã đặt. Không lấy smoke 2 phút thay cho soak 24 giờ.
