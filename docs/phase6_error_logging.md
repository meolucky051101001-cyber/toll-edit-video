# Phase 6 — bổ sung log cho lỗi đang bị nuốt

Ngày nghiệm thu: 01/11/2026. Nhánh `codex/phase-6-error-logging`, tạo sau khi
tích hợp Phase 5 fast-forward vào `tool-v1` tại `3b633ff`.

## Phạm vi thay đổi

Chỉ đổi handler có thân duy nhất là `pass` trong tám file của kế hoạch sang log,
và thu hẹp bốn bare except của code ứng dụng thành `except Exception`.
Không đổi retry, thứ tự pipeline, điều kiện QC, return, raise hoặc fallback hiện
có. Không cài/sửa venv, không cutover/rollback hoặc dùng lệnh điều khiển tiến trình.

| File backend | Handler im lặng trước | Sau | WARNING mới | DEBUG mới |
| --- | ---: | ---: | ---: | ---: |
| workflow_api.py | 43 | 0 | 4 | 39 |
| main.py | 27 | 0 | 1 | 26 |
| tool_control.py | 24 | 0 | 0 | 24 |
| video_utils.py | 21 | 0 | 19 | 2 |
| segment_editor_service.py | 18 | 0 | 3 | 15 |
| ai/translation.py | 17 | 0 | 12 | 5 |
| job_tracker.py | 13 | 0 | 10 | 3 |
| batch_processor.py | 12 | 0 | 11 | 1 |
| **Tổng** | **175** | **0** | **60** | **115** |

Giảm 100%, vượt yêu cầu 80%. Các nhánh optional, thử parser thay thế, tìm worker
context và cấu hình console dùng DEBUG để tránh cảnh báo cho fallback bình
thường. Các lỗi trong pipeline chính, kiểm tra khóa workflow, bảo vệ cleanup,
clip bị bỏ qua, probe duration và ghi SRT/backup segment dùng WARNING. DEBUG
có thể không xuất hiện khi logger đang ở mức INFO.

Log mới chứa ngữ cảnh tĩnh của module/hàm/thao tác và **tên lớp exception**,
ví dụ `PermissionError`, với `exc_info=False`. Không truyền exception object,
chuỗi lỗi, URL, header, input, response hoặc traceback vào log mới để tránh lộ
API key/token/mật khẩu proxy. Đổi lại, cần ngữ cảnh và loại exception để tra cứu;
Phase 6 không bật log payload thô.

Logging ở main/translation được khởi tạo sớm hơn để handler lúc import không
tham chiếu logger chưa tồn tại; tool_control bổ sung module logger.

Quét AST toàn backend trước sửa thấy 412 handler chỉ có pass và 12 bare except,
khác số liệu cũ trong kế hoạch. Sau sửa còn 237 handler ngoài tám file và tám
bare except trong `backend/scripts_and_tests` (script thử nghiệm/maintenance).
Không sửa các script này hoặc các handler có fallback thực hiện công việc khác
ngoài `pass` trong Phase 6.

## Các hành vi cần người dùng quyết định — chưa sửa

Các điểm sau có sẵn trước Phase 6. Thêm log chỉ làm lộ tình trạng, không tự đổi
chính sách xử lý lỗi:

| Ưu tiên | Vị trí | Hành vi hiện tại và tác động | Quyết định cần chốt |
| --- | --- | --- | --- |
| P1 | `main.py:api_workspace_cleanup`, handler dòng 2243; `workspace_usage_service.py:cleanup_jobs` | Khi đọc tracker lỗi, danh sách active có thể rỗng/thiếu rồi vẫn gọi cleanup_jobs. Hàm cleanup chỉ dùng danh sách này để loại job đang chạy, nên job đó có thể bị dọn nếu người dùng chọn. | Có từ chối thao tác dọn khi chưa xác minh được danh sách job active không? |
| P1 | `workflow_api.py:_wait_pipeline_free`, các handler khoảng 3934–3951 | Hàm tham chiếu `job_tracker` nhưng file không import/bind tên này; kiểm tra batch có thể thành NameError rồi tiếp tục. Nhánh stop dùng `_ss.WORKSPACE`, trong khi shared_state thay module bằng instance không gán thuộc tính này. Khi kiểm tra khóa/tracker lỗi, hàm vẫn có thể trả True. Hai caller gọi hàm nhưng không kiểm tra kết quả False khi hết timeout. | Có sửa binding và chặn job khi không xác minh được trạng thái khóa/stop hoặc hết timeout không? Cần lượt sửa chức năng riêng. |
| P1 | `segment_editor_service.py:regenerate_segments`, handler dòng 794 | Ghi translated.srt lỗi nhưng vẫn tăng revision, ghi draft và trả status success; draft/audio/SRT có thể lệch nhau. | Có yêu cầu ghi SRT thành công/transaction trước khi báo success không? |
| P2 | `segment_editor_service.py:get_audio_duration_seconds`, handler dòng 213 | ffprobe lỗi thì ước lượng duration = số byte / 16000 thay vì đo theo định dạng audio. Kết quả đi vào kiểm tra độ dài/atempo của segment. | Có bỏ ước lượng này, thay bằng decoder khác hoặc chặn khi chưa đo được duration không? |
| P2 | `segment_editor_service.py:regenerate_segments`, handler dòng 727 | Copy audio segment cũ sang .bak lỗi vẫn tiếp tục tạo/thay audio mới; backup tại đường dẫn này có thể thiếu. | Có bắt buộc backup thành công trước khi ghi đè audio segment không? |
| P2 | `workflow_api.py:_apply_timeline_edits_file`, handler dòng 2769 | Clip có start/end không parse được bị bỏ qua; nếu còn clip hợp lệ khác, render vẫn tiếp tục. | Có từ chối payload thiếu hợp lệ thay vì lược clip không? |
| P2 | `video_utils.py`, `batch_processor.py`: các handler đọc get_frozen_config | Khi đọc snapshot lỗi, tiếp tục với cấu hình/fallback hiện có; tùy nhánh có thể không giữ lựa chọn separator/ducking/voice của snapshot. | Có chặn riêng job đã có snapshot nhưng không đọc được không? |
| P2 | `ai/translation.py:load_custom_glossary`, handler dòng 212 | File glossary đọc/parse lỗi thì thử đường dẫn kế tiếp, cuối cùng dùng dict rỗng; thuật ngữ tùy chỉnh có thể bị bỏ. | Có đưa cảnh báo lên UI hoặc chặn khi người dùng đã chọn glossary không? |

## Bằng chứng kiểm tra

- So sánh AST với `3b633ff` sau khi loại phần log/khởi tạo logger mới và chuẩn
  hóa bốn bare except: toàn bộ tám file tương đương. Không có thay đổi AST khác
  về điều kiện, giá trị return, raise, await hoặc thứ tự gọi pipeline.
- Kiểm tra toàn bộ 175 log mới: đối số động duy nhất là tên lớp exception,
  không có traceback hoặc payload lỗi thô.
- Năm test trong `tests/test_v1_suppressed_error_logging.py` dùng dependency giả
  và dữ liệu tạm: glossary vẫn dùng candidate kế tiếp; lỗi tạo thư mục tracker
  vẫn nonfatal; đo duration vẫn dùng ước lượng cũ khi probe lỗi; lỗi đọc khóa
  vẫn giữ fallback cũ và phát cảnh báo; lỗi tracker trong cleanup vẫn gọi hàm
  cleanup với danh sách bảo vệ cũ. Hàm cleanup được mock hoàn toàn, không xóa
  dữ liệu. Đồng thời xác nhận payload có credential giả không xuất hiện trong log.
- Không sửa test cũ hoặc thêm skip. Mốc trước Phase 6: 1095 passed / 0 failed /
  79 skipped / 185 subtests trên Python 3.10.
- Bằng chứng chi tiết và log test đặt tại `scratch/phase6`, được ignore.

## Kết quả nghiệm thu bản cuối

| Kiểm tra | Kết quả |
| --- | --- |
| Test hồi quy riêng Python 3.12 | 5 passed; 3 warnings; 1.73 giây |
| Full suite Python 3.10 | 1100 passed / 0 failed / 79 skipped / 185 subtests passed; 3 warnings; 124.20 giây |
| Full suite Python 3.12 | 1100 passed / 0 failed / 79 skipped; 6 warnings; 108.30 giây |
| So với trước Phase 6 | Tăng 5 test; số failed/skipped không tăng |
| AST và dữ liệu log | 8 module đạt; 175 → 0 handler im lặng; 60 WARNING / 115 DEBUG; không payload/traceback trong log mới |

Full suite bản cuối nằm ở `scratch/phase6/pytest-py310-verified.log` và
`pytest-py312-verified.log`, đều exit 0. Python 3.12 chưa cài pytest-subtests nên
không có bộ đếm subtest riêng. API dashboard trả HTTP 200 khi kiểm tra chỉ đọc.
Không sửa requirements, lock hoặc script chuyển môi trường trong Phase 6.

Các test chỉ xác nhận phần log và hành vi cũ; chúng không chứng minh các rủi ro
trên đã được sửa. Không chuyển sang Phase 7 hoặc tự thực hiện các quyết định
chức năng. Ưu tiên người dùng duyệt xử lý ba điểm P1 trước bước refactor lớn.
