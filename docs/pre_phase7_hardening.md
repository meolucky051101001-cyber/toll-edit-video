# Sửa tồn đọng và chuẩn bị Phase 7 — 01/11/2026

Nhánh `codex/pre-phase7-hardening`, xuất phát từ Phase 6 (`fbd4fba`). Lượt này
thực hiện yêu cầu sửa lỗi tồn đọng rồi chuẩn bị Phase 7. Chưa tách các hàm lớn,
chưa merge vào `tool-v1`, chưa push hoặc chuyển môi trường production.

Đây là mốc chuẩn bị trước refactor. Kết quả triển khai tiếp theo nằm trong
[báo cáo Phase 7](phase7_function_refactor.md).

## Các lỗi chức năng đã xử lý

| Tồn đọng từ review Phase 6 | Hành vi sau sửa | Bằng chứng hồi quy |
| --- | --- | --- |
| Cleanup vẫn chạy khi không đọc được tracker | Đọc trạng thái/khóa theo chế độ strict; chưa xác minh được thì trả HTTP 503 trước khi gọi cleanup. Bảo vệ tên video và job ID của worker active; bỏ qua tên video đã hoàn tất. Xóa file lỗi trả error thay vì ok. | Tracker JSON hỏng/thiếu trường, batch lock còn sống, active worker, cleanup thất bại; chỉ xóa fixture tạm. |
| Tác vụ thủ công bỏ qua lỗi khóa và timeout | Import tracker đúng, dùng WORKSPACE của workflow, kiểm tra cả worker; lỗi kiểm tra chặn tác vụ. Chỉ dọn stop cũ sau khi xác nhận idle. Hai caller truyền job_id và tôn trọng False khi timeout. | Busy/timeout không dọn stop hoặc chạy disk check; stop không xóa được thì chặn; CN/translate không gọi stage tiếp theo khi timeout. |
| Ghi SRT lỗi nhưng tăng revision thành công | Chuẩn bị toàn bộ audio/SRT/draft trong staging; snapshot bản cũ trước commit, thay từng file bằng atomic copy, rollback cả file đang thử ghi khi có lỗi. Chỉ báo success/cache sau commit đủ. | Tiêm lỗi ở SRT chính/phụ/draft, lỗi sau replace, TTS câu thứ hai lỗi: audio và metadata cũ còn nguyên. |
| Duration suy từ số byte khi ffprobe lỗi | Dùng decoder soundfile dự phòng; chỉ nhận duration dương, hữu hạn. Không đo được thì AudioDurationError, không cache ước lượng. | WAV stereo 48 kHz đo đúng 0,5 giây; ffprobe lỗi/NaN; dữ liệu audio hỏng bị chặn. |
| Backup audio lỗi vẫn ghi đè | Backup bắt buộc hoàn tất trước TTS/thay file. Backup cũ rỗng/không phải file bị từ chối. | Backup lỗi: chưa gọi TTS, chưa thay audio/SRT/draft. |
| Clip timeline không hợp lệ bị bỏ qua | Giá trị không parse được hoặc NaN/Infinity làm toàn yêu cầu thất bại trước render. Giữ cách clamp các mốc hữu hạn hợp lệ. | Payload có một clip hợp lệ và một clip lỗi vẫn không render. |
| Snapshot lỗi thành cấu hình hiện tại | Store thiếu vẫn optional; store đã tồn tại nhưng lỗi đọc/JSON/schema thì FrozenConfigError. Separator/mixer/voice không dùng fallback hiện tại để che lỗi snapshot. | Store thiếu/hỏng, lookup scoped trước global, separator chưa chạy GPU khi snapshot lỗi; test migration cũ vẫn giữ. |
| Glossary lỗi bị bỏ qua qua provider khác | File thiếu cho phép tìm đường dẫn legacy; file hiện hữu lỗi đọc/schema chặn dịch trước chọn provider. Thư mục ở đường dẫn glossary cũng là lỗi. | Không thử provider hoặc candidate sau khi glossary hiện hữu lỗi; log không chứa credential giả. |

Các lỗi integrity mới được phân loại permanent trong retry policy để lỗi bọc
chuỗi timeout không bị đưa vào hàng retry mạng. QC BLOCK, quy tắc OCR và lịch
retry mạng của các phase trước giữ nguyên. Hai fixture worker cũ đã bổ sung
`active=True` đúng schema; không bỏ assertion hoặc thêm skip.

Lỗi decoder còn được truyền xuyên qua hàm đọc segment V1/V2 để không trả về
danh sách thiếu các câu phía sau rồi ghi SRT từ danh sách không đầy đủ. Test
tiêm lỗi ở câu thứ hai xác nhận chặn toàn yêu cầu, không trả danh sách một phần.

Commit nhiều file ở đây bảo vệ lỗi I/O bắt được trong cùng tiến trình, **không
phải transaction nguyên tử nhiều file khi mất điện/kill tiến trình**. Thư mục
`.segment-regenerate-*` giữ journal `rollback/recovery.json` và các bản gốc nếu
rollback lỗi hoặc tiến trình bị ngắt trong lúc commit; cần kiểm tra/khôi phục
thủ công trước khi dùng job đó. Không có cơ chế tự recovery sau crash trong lượt
này. Khóa asyncio hiện tại chỉ bảo vệ trong tiến trình, chưa thay bằng khóa
liên tiến trình cho toàn bộ editor. Kiểm tra idle/active cũng là snapshot trạng
thái, không phải cơ chế đặt chỗ tài nguyên xuyên tiến trình mới.

## Khắc phục đường dẫn package local của Phase 5

`scripts/export_capcut_wheel.py` xuất wheel pure Python từ package CapCut 1.0.0
đang cài, kiểm tra SHA256 và size từng file theo installed RECORD, bỏ metadata
cài đặt/launcher/bytecode, rồi tạo RECORD mới. Từ chối nếu file bị sửa hoặc có
wheel khác cùng tên. Hai test kiểm chứng archive lặp lại cho cùng byte và từ
chối package bị sửa.

- Archive: `backups/wheels/capcut_tts_api-1.0.0-py3-none-any.whl` (được ignore).
- SHA256: `a7b42630773a6aa44a2a076283ef315f43bd25ac1bbb193618d949163e3e2024`.
- Đã cài lại offline duy nhất trong venv_py312, `--no-index --no-deps
  --force-reinstall`; 8 file source có hash trước/sau giống hệt.
- pip check sạch; lock 149 dòng khớp pip freeze, tham chiếu wheel local kèm hash.

Archive này phục hồi từ package đang cài, không phải bằng chứng xác thực nguồn
nhà phát hành hoặc wheel gốc giống từng byte. Cần sao lưu archive riêng khi
chuyển máy. CUDA wheel index, Python base trong cache Codex và đường dẫn nhúng
launcher sau rename vẫn cần thao tác theo [hướng dẫn Phase 5](phase5_python312.md).
Không sửa venv Python 3.10 hoặc các model venv.

## Golden baseline trước refactor

`tests/test_phase7_orchestrator_golden.py` chạy execute_job thật với manifest,
serialization artifact, tạo SRT và ASS thật. Mock các stage nặng và các side
effect runtime: ASR/OCR/TTS/separation/mix/render/QC, tracker/history/cleanup.
Tất cả dữ liệu là fixture tạm; không dùng video, API key hoặc job production.
Phép đo font được cố định để không phụ thuộc font cài trên máy.

Ba contract:

1. Thành công: đối chiếu chính xác thứ tự stage, 15 artifact, toàn bộ
   translation.json và final.ass; chạy lại job completed không gọi stage nặng.
2. Mixer thất bại rồi resume: không lặp ASR/OCR/translate/TTS; byte của
   translation.json và final.ass không đổi.
3. QC BLOCK: không deliver/publish/completed, video đã xuất trước đó còn nguyên.

Fixture `tests/fixtures/phase7/execute_job.json` được chụp từ orchestrator/ASS/OCR
chưa refactor, SHA256
`ee4e5a27365ebb2cded35198a545b1661248026ac1529729dda2ef283eb9bb57`.
Không tự cập nhật expected khi test fail. `TOOL_V1_CAPTURE_GOLDEN=1` chỉ ghi bản
actual vào scratch để so sánh, vẫn assert against fixture cố định.

```powershell
.\backend\venv_py312\Scripts\python.exe -B -m pytest tests/test_phase7_orchestrator_golden.py -q -p no:cacheprovider
```

## Trình tự Phase 7 sau bàn giao này

1. Tạo nhánh Phase 7 từ commit hardening đã nghiệm thu; giữ baseline fixture.
2. Tách execute_job trước, giữ đúng thứ tự stage thực tế (OCR trước translation),
   checkpoint/resume, cache, voice, mixer, QC/delivery và exception semantics.
   Các helper theo kế hoạch dưới 150 dòng; không đổi thứ tự theo tên helper.
3. Sau đó lần lượt generate_ass_file, perform_video_ocr,
   process_single_local_video. Mỗi hàm một commit theo kế hoạch; golden và full
   suite phải pass trước khi chuyển hàm.
4. Sau refactor, người dùng bấm thử một video dưới 30 giây trên dashboard để
   đối chiếu phụ đề, giọng, mixer, OCR mask và QC. Golden dùng mock không thay
   thế nghiệm thu video thật.

Chưa bắt đầu refactor trong lượt chuẩn bị. Các cảnh báo deprecation hiện có
như FastAPI on_event thuộc phạm vi Phase 8.

## Nghiệm thu

Baseline trước lượt này: 1100 passed / 0 failed / 79 skipped; Python 3.10 có
185 subtests passed. Bổ sung 36 test: 31 test guards/config/duration/commit,
3 golden test và 2 test archive wheel; giữ nguyên 79 skip có sẵn.

| Kiểm tra | Kết quả bản cuối |
| --- | --- |
| Full suite Python 3.10 | 1136 passed / 0 failed / 79 skipped / 213 subtests passed; 3 warnings; 174,08 giây |
| Full suite Python 3.12 | 1136 passed / 0 failed / 79 skipped; 6 warnings; 101,82 giây |
| Golden trước refactor | 3 passed; đã có trong full suite |
| venv_py312 | pip check sạch; lock khớp chính xác freeze 149 dòng |
| Kiểm tra tĩnh | Python parse hợp lệ; git diff --check sạch; PS 5.1 parser chấp nhận script chuyển môi trường, chưa thực thi script |

Preflight Python 3.12 đạt ready=true, 25/25 pass. AUTODUB_WORKSPACE và
AUTODUB_OUTPUT_DIR chỉ override trong process tới `scratch/prephase7` vì
preflight tạo/xóa file kiểm tra quyền ghi; chưa xác nhận quyền ghi production
hoặc render thật. API status HTTP 200; PID cổng 8088=18000, 8090=10932 giữ nguyên
khi kiểm tra chỉ đọc. Chưa chạy cutover/rollback, chưa restart dịch vụ.

Log và bằng chứng runtime nằm trong `scratch/prephase7` (ignore). Chỉ source,
test, fixture tổng hợp và tài liệu được commit; không commit dữ liệu job/wheel.
Hai log nghiệm thu bản cuối là `pytest-py310-verified.log` và
`pytest-py312-verified.log`, đều exit 0. Python 3.12 chưa cài pytest-subtests nên
không có bộ đếm subtest riêng. Không thêm hoặc thay package test chỉ để đồng bộ
bộ đếm giữa hai môi trường.
