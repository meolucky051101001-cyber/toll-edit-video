# Review toàn dự án Tool V1 — 17/11/2026

**Kết luận:** đợt sửa ngày 16/11 đã cải thiện rõ local và Cloud. Local đã có video blur được nghiệm thu; Cloud đã qua canary và chạy thử phase 8. Tuy nhiên còn một lỗi P1 ở khôi phục hàng đợi, các lỗi P2 về xóa job, blur VFR, health và che chữ Cloud. Chưa nên coi toàn bộ dự án là release đã khép hết lỗi.

Review này đọc mã, đối chiếu lịch sử/artifact, chạy kiểm thử và tạo bằng chứng cô lập. Không sửa mã sản phẩm, không triển khai, không gọi provider trả phí, không gửi Telegram.

## Phạm vi và mốc thời gian

- Thời điểm review: sáng 17/11/2026, múi giờ UTC+7. Các commit mới nhất được tìm thấy thuộc ngày 16/11; chưa thấy commit ngày 17/11 trước review.
- Local: `C:\tool v1`, nhánh `codex/toolv1-local-blur-stability-20261116`, HEAD `9dbfc0a6b61dd195895905de1e068b5168f2d8a7`. Checkout sạch trước review. So với `4b2d234`: 85 file, 9.919 dòng thêm và 921 dòng xóa; sáu commit local trong ngày 16/11.
- Rà các luồng orchestrator, OCR/ASR/dịch/TTS, mask/render/QC, API/Studio, queue/recovery, shutdown, đóng gói; chạy toàn bộ `tests/` trên Python chính.
- Cloud mới nằm ở `C:\tool_v1_clean\cloud-phase0-1-20261114`, nội dung tương ứng `be62883c57baa57bbece7dae36db42370a8c3e28`. Đã đối chiếu 1.258 file tracked của commit này với nội dung trên đĩa sau chuẩn hóa newline: không lệch. 58 file trong receipt live cũng khớp SHA256 byte. HEAD/index checkout vẫn ở `35483a1`, nên Git báo dirty/untracked; không được nhầm trạng thái index với nội dung đã thử.
- Cloud trong checkout local và runner tại `C:\Projects\Tools\Tool V1\Updates` là bản khác với Cloud phase 8. Các kết quả của hai bản không thay thế nhau.

## Những lỗi còn lại, theo ưu tiên

### R1 — P1: Khôi phục lỗi có thể ghi đè mất hàng đợi

`backend/telegram_bot.py:1852` bắt mọi lỗi đọc/parse/khôi phục snapshot và trả `0`. `backend/tool_control_runtime.py:89` sau đó bỏ `recovery_pending` và publish bộ nhớ hiện tại, có thể rỗng hoặc chỉ phục hồi một phần. File snapshot cũ bị thay thế dù phục hồi chưa thành công.

Tái hiện bằng hàm thật trích AST, `MonitoredQueue` thật và thư mục tạm: JSON hỏng → restore trả `0` → publish ghi `{"active_job": null, "items": []}`. Đường phát sinh thực tế tồn tại: `backend/main.py:1456` ghi snapshot khi đổi thứ tự bằng `write_text`, không atomic; reader có thể đọc đúng lúc file chưa hoàn tất.

Cần phân biệt "không có job" với "phục hồi thất bại", giữ snapshot và trạng thái chưa phục hồi khi lỗi; chỉ publish sau khi phục hồi hoàn chỉnh. Cần đồng bộ/atomic hóa writer đổi thứ tự.

Bằng chứng: [script](../scratch/review_runtime_probe_20261117.py), [output](../scratch/review_runtime_probe_20261117.txt).

### R2 — P2: Job đã xác nhận xóa có thể được chạy lại

`backend/telegram_bot.py:1803` đã bỏ `filter_restored()` khi nạp snapshot. `MonitoredQueue.get_nowait()` chỉ xử lý delete còn `pending`, nên lệnh đã `applied` hoặc `already_absent` không ngăn job trong snapshot cũ quay lại.

Repro: operation `applied` + snapshot chứa job đó → restore và claim thành công chính job đã xóa. Snapshot cũ có thể được ghi trở lại khi thao tác reorder đọc trước nhưng ghi sau lúc bot publish xác nhận xóa. Cần giữ filter theo identity qua restart và đồng bộ các writer snapshot.

Bằng chứng dùng chung script/output của R1; output có `APPLIED_DELETE_RESTORED_AND_CLAIMED deleted`.

### R3 — P2: Blur sai thời điểm với video có tốc độ khung hình thay đổi

`backend/ocr_utils.py:640` đổi PTS thành `round(time * fps)`; refinement tại dòng 763 seek theo số frame và gán lại `index / fps`. Với VFR, đây không phải cùng một trục thời gian. OCR proxy vẫn giữ VFR, không loại bỏ điều kiện này.

Clip CPU tổng hợp 30 frame, nhịp 30 → 10 fps, OCR giả để cô lập timing: caption rộng bắt đầu ở PTS **0,500s**, mask rộng bắt đầu **0,580s**. Chênh **80ms** đủ để hở chữ nguồn. Frame 15 có PTS 0,500s nhưng phép chia theo FPS trung bình cho 0,967s.

Cần dùng PTS thật cho từng frame và cho intervals, hoặc chuẩn hóa CFR với hợp đồng thời gian rõ ràng. Repro này xác minh logic timing/mask, không đo độ chính xác model OCR.

Bằng chứng: [script](../scratch/review_vfr_20261117.py), [result.json](../scratch/review-vfr-20261117/result.json).

### R4 — P2: Health báo thiếu toàn bộ router đang hoạt động

`backend/main.py:105` kiểm tra `.path` trực tiếp trên `app.routes`. FastAPI **0.162.0** đang cài lưu router include bằng `_IncludedRouter`, không có `.path`, nên bảy component đều bị báo `false`.

Repro offline chỉ trích hàm health và tạo app nhỏ, không import/start production: GET ASGI `/quy-trinh` trả **200**, OpenAPI có đủ bảy path, nhưng health vẫn `ready=false`. Snapshot HTTP runtime cũng báo đúng mẫu bảy component false. Đây là lỗi cách đo health; không phải bằng chứng cả bảy tính năng thực sự hỏng.

Cần kiểm tra route hiệu lực theo API tương thích hoặc lưu trạng thái đăng ký router. Thêm kiểm chứng cả trường hợp đủ router, vì test hiện chỉ kiểm nhánh thiếu router.

Bằng chứng: [script](../scratch/review_health_20261117.py), [JSON](../scratch/review-health-20261117.json).

### R5 — P2: Cloud phase 8 vẫn hở chữ nguồn dù QC PASS

Trên **Cloud mới `be62883`**, output job `cloud-20261116-111516-f05970` còn chữ Trung ở hai mép hộp Việt tại **0,500s** và **18,500s**. Đã trích nguồn/thành phẩm cùng timestamp và kiểm SHA256 output khớp receipt.

Đường mã trong checkout Cloud:

- `backend/cloud/gpu_scripts/full_pipeline.py:859`: không tìm được OCR thì đặt `best_block=None`, `no_caption_evidence` rồi tiếp tục.
- `backend/cloud/gpu_scripts/subtitle_timing.py:103`: bỏ qua mask thiếu geometry; dòng 260 tiếp tục tạo hộp theo chữ Việt, không bảo đảm phủ câu nguồn dài hơn.
- `backend/cloud/cloud_orchestrator.py:563`: đánh `passed=True` sau kiểm tra tải/count/hash/probe, chưa có kiểm tra pixel chứng minh chữ nguồn đã được che.

Artifact live chỉ có 3/15 cue được located; 12 cue thiếu caption evidence. Không coi thiếu evidence là đã xác minh không có caption. Cần bổ sung nhận diện/review và QC hình ảnh cho những cue này trước khi mở rộng Cloud. Không quy finding này cho bản blur local `40ea82d`.

Bằng chứng: [ảnh đối chiếu](../scratch/review_20261117_cloud/cloud_caption_comparison.png), [biên bản visual QA](../scratch/review_20261117_cloud/visual_qa.json).

### R6 — P2: Shortcut Cloud chính vẫn chạy bản cũ

`start_cloud_dashboard.bat:17` hardcode `C:\Projects\Tools\Tool V1\Updates` rồi chạy `run_cloud_node.py` ở đó. Runner này và orchestrator cũ không phải bộ phase 8 đã nghiệm thu. Bản phase 8 có launcher tương đối theo thư mục riêng, nhưng chưa được launcher ở local chính sử dụng.

Cần chốt bản phát hành/đường chạy Cloud có thẩm quyền và cập nhật điểm khởi động trong bước triển khai. Việc đã có sáu nhánh stage 8 hoặc canary đạt không chứng minh shortcut hiện hành chạy bản đó.

## Kết quả kiểm chứng mới trong lượt review

| Kiểm tra | Kết quả | Giới hạn |
|---|---|---|
| Python 3.12.14, toàn bộ `tests/` local | **1.484 passed, 2 failed, 77 skipped**, 132,99s | Đã chạy xong; không đổi test/quarantine |
| Chạy riêng hai test fail | **Cùng 2 failed** | Không phải chỉ do thứ tự full suite |
| Node `tests/js/*.test.js` | **13 passed** | Frontend logic/wrapper |
| Cloud mới `test_cloud_phase8_rollout.py` | **6 passed**, 0,97s | SQLite tạm, adapter giả, gồm crash/recover; 0 CU |
| Python/dependency/preflight | **3.12.14; pip check PASS; preflight 25/25** | Kiểm tra chỉ đọc |
| Dashboard ngoài sandbox | HTTP trả lời, `idle`, `active=false`; health FAIL | Health bị R4 làm sai; RAM snapshot 79,4%, VRAM 0,88/6GB |
| Tool Control | TCP 8090 lắng nghe | Chưa có `/health`, không coi TCP là đủ sức khỏe ứng dụng |
| Package dry-run | **618 file, 10,31 MB** | Chưa tạo release mới trong lượt này |
| ZIP accepted 16/11 | **618 entries, CRC tốt, SHA256 khớp receipt** | Commit nguồn `9dbfc0a` |
| `git diff --check 4b2d234..HEAD` | PASS | Không chứng minh đúng nghiệp vụ |

77 skip gồm **72 quarantine**, hai Remotion opt-in và ba thiếu benchmark fixture. Không suy từ pass count thành phần trăm hoàn thành dự án. Python 3.10 không chạy lại toàn bộ trong lượt này; CI/hồ sơ hai runtime trước đó là bằng chứng lịch sử.

Hai test fail cần cập nhật theo hợp đồng đã chọn, đồng thời giữ ý nghĩa regression:

1. `test_v1_gemini_stability_codex.py:181`: fixture trả câu **7 từ** trong khi `target_words=5`; validator mới từ chối đúng giới hạn. Test cần một câu hợp lệ trong ngân sách để kiểm partial response, cùng ca riêng kiểm vượt ngân sách.
2. `test_v1_phase9_audit.py:91`: chỉ mock `get_status(active=True)`, không giữ batch lease thật. Guard mới claim lease nên request đi tiếp và nhận JSON lỗi file đầu vào (HTTP 200), thay vì 409 test mong đợi. Test tranh chấp cần tạo quyền sở hữu thật; các regression lease mới đã đạt. Failure này tự nó không chứng minh hai job có thể chạy cùng lúc.

Runner ban đầu trong sandbox bị treo tại `asyncio` socketpair, đã tái hiện bằng stdlib tối giản và dừng. Kết quả hoàn tất bên trên chạy ngoài sandbox với mạng ngoài bị chặn và fixture workspace tạm; các lượt dở không tính PASS.

Bằng chứng mới: [full-suite log](../scratch/review_20261117/full-suite.log), [JUnit](../scratch/review_20261117/full-suite.xml), [hai test chạy riêng](../scratch/review_20261117/two-failures.log), [Cloud phase 8](../scratch/review_20261117/cloud-phase8.log).

## Tiến độ đã hoàn tất và còn thiếu

| Nhóm | Đã có bằng chứng | Việc còn lại |
|---|---|---|
| Runtime/API/GPU | Lease dùng chung; drain native task trước nhả tài nguyên; scoped shutdown; explicit ENV và giọng theo job | Sửa R4 và duy trì test theo runtime thật |
| OCR/blur local | Geometry v8, blur v4; source lifetime; bỏ reuse OCR trên ảnh thay đổi; refinement biên; QC thêm biên | R3; bộ mẫu rộng hơn: VFR, caption rất ngắn, hai dòng/sát mép, crop/mirror |
| Video local nghiệm thu | 18 câu Gemini/CapCut Mai, 194,28s; QC 15 PASS với 143 frame; decode toàn file đạt | Không suy thành nghiệm thu mọi video hay lip-sync từng từ |
| Cấu hình local | Đọc lại thấy `localized_blur`, `V1_VISUAL_SUB_TO_SPEECH=true` cho job mới | Snapshot job cũ giữ riêng; render lại nếu muốn áp dụng cho video cũ |
| ASR/dịch | Caption evidence và chốt rút gọn đã được sửa; 16 chỉnh dịch trong bộ 5 job/77 cue | Chưa có bằng chứng TTS/render lại cho 16 câu ứng viên; audio cũ không chứng nhận bản dịch mới |
| Queue/recovery | Có snapshot, delete operations, recovery và test nền | Sửa R1/R2 trước khi coi restart hàng chờ là an toàn |
| Cloud phase 7 | Ba mẫu đã nghiệm thu, 59/59 TTS, 6/6 tiêu chí; giảm số dư quan sát 4,20/5 CU; GPU đã thu hồi | Phạm vi mẫu cũ, không bảo đảm mọi đầu vào |
| Cloud phase 8 | Sáu nhánh triển khai; live 15/15 dịch/TTS, output 23,5s; cleanup verified; rollout paused | R5/R6; cập nhật runbook bằng evidence live và chốt chất lượng mẫu mới |
| Release/tài liệu | ZIP accepted có receipt/hash; lịch sử commit local sạch | Full suite còn đỏ; tài liệu trạng thái tổng quan cũ, Cloud index/HEAD chưa phản ánh tree đang dùng |

Lượt phase 8 live có mức giảm số dư **0,27 CU / trần 1 CU**; ledger **0,276 CU** là ước tính runtime, không phải billing thực tế đã chốt. Hồ sơ cuối lượt ghi không còn session/assignment hay pending cleanup. Đây là bằng chứng đã lưu ngày 16/11, không phải truy vấn provider mới trong review này.

Evidence Cloud: `C:\tool_v1_clean\cloud-phase0-1-20261114\scratch\phase8_live_20261116\summary_native-2.json`, `code_receipt.json`, và thư mục `results\cloud-20261116-111516-f05970\attempt_1`. Runbook phase 8 chưa ghi đầy đủ lượt live này.

Thứ tự làm tiếp: **khóa lỗi mất/khôi phục job → sửa health và hai test lệch hợp đồng → sửa VFR/che chữ Cloud, kiểm bằng pixel → chốt launcher/bản Cloud → hoàn tất TTS/render cho 16 câu dịch và nghiệm thu bộ mẫu mở rộng**. Giữ các báo cáo 16/11 như lịch sử; không dùng kết luận đầu ngày để phủ nhận phần đã sửa cuối ngày.
