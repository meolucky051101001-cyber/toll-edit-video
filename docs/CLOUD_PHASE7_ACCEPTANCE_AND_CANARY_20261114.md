# Hồ sơ Nghiệm thu Giai đoạn 7 — Ma trận Kiểm thử Lỗi & Cổng Sẵn sàng Canary GPU

- **Ngày lập**: `2026-11-14`
- **Nhánh / Checkout triển khai**: `C:\tool_v1_clean\cloud-phase0-1-20261114` (nhánh `codex/cloud-phase0-1-20261114`)
- **Trạng thái bật Cloud mặc định**: `V1_CLOUD_MODE = False` (`backend/v1_feature_flags.py`)
- **Chốt Canary thật 16/11/2026**: **ĐẠT sáu tiêu chí trong phạm vi nghiệm thu đã ghi**, không còn P0/P1 đã xác minh trên ba mẫu; đủ điều kiện triển khai Giai đoạn 8. Chưa bật phát hành Cloud. Mục 1–9 là lịch sử; mục 10 là kết quả hiện tại.
- **Bản sửa hiện tại**: job `cloud-20261115-232507-db8a2a` hoàn tất ba video, **59/59 CapCut TTS**, full decode/QC/ASS/TTS audit đạt. Hồi quy **324 passed, 1 skipped**, mock Telegram kiểm tra riêng **1 passed** sau đó. Chi phí quan sát **4,20 CU / 5 CU**, GPU đã thu hồi.
- **Đối chiếu nội dung/timing**: max drift ranh giới nhóm **0,201 s**; sửa sai nghĩa/giá/prefix và ASS kết thúc sớm. Người dùng xác nhận đoạn 30,4 s không có lời riêng, bốn clip thành phẩm nghe rõ và giữ cách đọc. Không coi review theo mẫu là chứng nhận timing từng từ hay mọi video.
- **TTS mặc định trên Cloud**: Giữ nguyên **CapCut TTS (`capcut_tts_api`)**, không tự đổi sang Edge TTS.
- **Mục tiêu Giai đoạn 7**: Xây dựng ma trận kiểm thử lỗi toàn diện trong môi trường cô lập (`0 CU`, không đụng GPU thật hay DB/thành phẩm V1), khóa các lỗi biên phát hiện trong lúc đối soát ma trận, xây dựng cổng sẵn sàng Canary + bộ đối chiếu Local vs Cloud, và giữ trạng thái chờ người dùng phê duyệt danh sách video mẫu cùng hạn mức CU trước khi chạy Canary GPU thật.

---

## 1. Các lỗ hổng phát hiện và đã vá trong Giai đoạn 7

1. **Chuyển tiếp tùy chọn `--allow-no-speech` (`backend/cloud/cloud_orchestrator.py`)**:
   - Đã chuyển tiếp cờ `--allow-no-speech` vào `pipeline_argv` khi `job.options.get("allow_no_speech")` được bật; mặc định không bật cờ này để tiếp tục chặn video BGM-only / không có lời thoại với mã `NO_SPEECH_DETECTED`.
2. **Bảo vệ ranh giới hủy muộn tại giai đoạn `validating` (`backend/cloud/cloud_orchestrator.py`)**:
   - Bổ sung kiểm tra `_raise_if_aborted()` trước và sau `validate_downloaded_output(...)`, đồng thời bọc vùng ghi `source_snapshot` / `output_manifest.json` sau `export_deliverable_atomic(...)`: nếu người dùng yêu cầu hủy ngay trong lúc xác thực hậu xuất file, tệp thành phẩm mới xuất (`final_video_path != dl_file`) sẽ được thu hồi (unlink) trước khi đánh dấu hủy, tránh giao thành phẩm nửa chừng của một job đang hủy.
3. **Tính idempotent khi hủy lặp lại (`backend/cloud/cloud_contract.py`)**:
   - `request_job_cancellation` trả về `cancelled=True, cancel_requested=True, idempotent=True, state="cancelled"` khi job đã ở trạng thái `cancelled`, giúp các lần bấm hủy hoặc retry HTTP từ UI/Telegram có hành vi idempotent rõ ràng mà vẫn giữ `cancelled=False` cho `completed` / `failed`.
4. **Phát hiện VM mồ côi trước `register_session` khi khôi phục (`backend/cloud/cloud_job_store.py`)**:
   - Bổ sung `_derive_expected_session_name(job_id, attempt)` và cập nhật `CloudJobStore.recover()` để khi `adapter` được truyền vào kiểm tra job dở dang chưa kịp `register_session`, hệ thống vẫn đối chiếu danh sách session đang hoạt động từ provider; nếu tên session dự kiến đang chạy trên Colab, bản ghi sẽ gắn lại `session_name`, dừng và xác nhận thu hồi session mồ côi đó thay vì bỏ sót. Vẫn giữ `0` lệnh gọi provider/WSL khi `recover()` chạy không có `adapter` và không có `session_name`.
5. **Race condition khử trùng lặp `X-Idempotency-Key` khi đồng thời (`backend/cloud/cloud_job_store.py` & `backend/cloud/cloud_contract.py`)**:
   - Khi nhiều request HTTP POST `/api/cloud/enqueue` gửi đồng thời cùng `X-Idempotency-Key` trong lúc job gốc vẫn ở trạng thái `queued`, `store.enqueue()` trả về job cũ nhưng logic cũ chỉ kiểm tra `job.state != "queued"`, khiến cả các request đến sau báo `deduplicated=False`.
   - Đã bổ sung `CloudJobStore.enqueue_or_get(...) -> Tuple[CloudJob, bool]` (giữ nguyên tương thích chữ ký `enqueue(...) -> CloudJob`) và tích hợp vào `admit_and_enqueue_job(...)` để chỉ request tạo job nhận `deduplicated=False`, còn các request trùng đồng thời nhận `deduplicated=True`.
6. **Che dấu Cookie và lỗi cleanup chứa bí mật (`backend/cloud/cloud_security.py` & `backend/cloud/cloud_orchestrator.py`)**:
   - Bổ sung `_COOKIE_HEADER_RE` (che `Cookie:` / `Set-Cookie:`) và `_COOKIE_KV_RE` (che `SAPISID`, `APISID`, `HSID`, `SSID`, `SIDCC`, `OSID`, `__Secure-*`) trong `redact_secrets`.
   - Che bí mật bằng `redact_secrets(...)` đối với `error` trả về từ `_verify_or_stop_session` và chuỗi lỗi log ra tại `_finalize_session_once` (`logger.error`), đảm bảo không lọt key/token/cookie vào log, DB, bảng `cloud_cleanup_events` hoặc báo cáo Canary.
7. **Cô lập tuyệt đối DB và provider khi chạy `tests/test_cloud_routes.py`**:
   - Bổ sung fixture autouse `_isolate_routes_store_and_provider` trỏ `cloud_routes.store` vào `tmp_path / "cloud_routes_test.db"`, stub `get_usage` / `get_sessions` và dọn `_WORKER_INSTANCE` cùng provider cache sau mỗi test để không đụng WSL hay DB vận hành.

---

## 2. Ánh xạ 10 Nhóm Ma trận Kiểm thử Lỗi Giai đoạn 7 (`0 CU`)

Toàn bộ ma trận kiểm thử nằm trong `tests/test_cloud_phase7_acceptance_matrix.py` (18/18 test case PASS ở chế độ cô lập với fake adapter + `tmp_path` SQLite):

| Nhóm | Phạm vi theo kế hoạch | Tên hàm kiểm thử | Kết quả |
|---|---|---|---|
| 1 | Đầu vào & Lệnh (tên có dấu nháy, Unicode, `&`, khoảng trắng, traversal, `-key` injection) | `test_p7_01_input_and_command_matrix` | PASSED |
| 2 | Đồng thời (2 worker tranh claim, 4 HTTP retry cùng `X-Idempotency-Key`, lease expiry, stale attempt 1 bị chặn) | `test_p7_02_concurrency_two_workers_http_retry_and_stale_attempt` | PASSED |
| 3 | Hủy ở mọi bước (`queued`, `provisioning`, `uploading`, `preparing`, `processing`, `downloading`, `validating_after_export`) & cleanup fail | `test_p7_03_cancel_matrix_every_state_from_queued_to_validating[...]` (7 tham số) & `test_p7_03b_cancel_cleanup_failure_never_marks_cancelled` | PASSED (8/8) |
| 4 | Phục hồi (crash trước tạo VM, sau tạo VM trước `register_session`, sau đăng ký, lease hết hạn, provider mất kết nối) | `test_p7_04_recovery_crash_before_after_vm_lease_expiry_and_provider_offline` | PASSED |
| 5 | Sổ chi phí (job `failed`, job `cancelled` + soft-delete vẫn giữ chi phí, retry nhiều attempt, vắt qua nửa đêm ICT) | `test_p7_05_cost_ledger_error_cancel_delete_retry_midnight_matrix` | PASSED |
| 6 | Chất lượng dịch & TTS (thiếu cue, sót CJK, audio TTS rỗng/hỏng, BGM-only, `--allow-no-speech`) | `test_p7_06_translation_and_tts_quality_gates_and_allow_no_speech_forwarding` | PASSED |
| 7 | Thành phẩm (giữ file legacy không ghi đè, chặn file tải cắt cụt với `qc_report.json`, lỗi đĩa đầy `ENOSPC` không để rác `.part`) | `test_p7_07_deliverable_collision_partial_download_and_disk_full` | PASSED |
| 8 | Giao diện / API (provider treo chậm không nghẽn đồng thời `/api/cloud/status`, hủy lặp lại idempotent) | `test_p7_08_ui_slow_provider_polling_and_repeated_cancel_idempotency` | PASSED |
| 9 | Local độc lập (`V1_CLOUD_MODE=False` và khi module Cloud hỏng/không import được vẫn không ảnh hưởng pipeline Local) | `test_p7_09_local_mode_unaffected_when_cloud_off_or_cloud_backend_broken` | PASSED |
| 10 | Không rò rỉ bí mật (`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `Cookie: SAPISID=...` không xuất hiện trong DB, logs, báo cáo Markdown) | `test_p7_10_secret_redaction_in_provider_errors_logs_db_and_artifacts` | PASSED |
| Canary Gate | Cổng chặn Canary khi thiếu duyệt/quá ngân sách/còn pending cleanup + bộ đối chiếu 6 tiêu chí Local vs Cloud (`cloud_canary.py`) | `test_p7_11_canary_readiness_gate_and_local_vs_cloud_parity_comparator` | PASSED |

---

## 3. Kết quả Kiểm thử Cô lập Trước Canary (`0 CU`)

1. **Ma trận nghiệm thu Giai đoạn 7** (`tests/test_cloud_phase7_acceptance_matrix.py`):
   - **18 passed** (`0 failed`, `0 skipped`, `11.17s`).
2. **Toàn bộ 10 bộ kiểm thử Cloud (`0 CU`, chạy cô lập)**:
   - `tests/test_cloud_job_store.py`
   - `tests/test_cloud_phase1_security.py`
   - `tests/test_cloud_phase2_lifecycle.py`
   - `tests/test_cloud_phase3_cost_ledger.py`
   - `tests/test_cloud_phase4_env_preflight.py`
   - `tests/test_cloud_phase5_quality_export.py`
   - `tests/test_cloud_phase6_api_dashboard_telegram.py`
   - `tests/test_cloud_phase7_acceptance_matrix.py`
   - `tests/test_cloud_routes.py`
   - `tests/test_telegram_cloud_integration.py`
   - Tổng kết: **76 passed** (`0 failed`, `0 skipped`, `23.71s`).
3. **Toàn bộ 8 bộ kiểm thử OCR / Phụ đề / QC Local**:
   - `tests/test_ocr_geometry_audit.py`, `tests/test_ass_cover_dimensions.py`, `tests/test_subtitle_tracking_regression.py`, `tests/test_v1_ocr_geometry_repair.py`, `tests/test_ocr_adaptive_sampling.py`, `tests/test_ocr_asr_correction.py`, `tests/test_dual_voice_turn_accuracy.py`, `tests/test_v1_qc_policy_source.py`
   - Tổng kết: **112 passed, 1 skipped** (`7.77s`; ca skip duy nhất là `test_source_lifetime_outside_speech_is_covered_without_extending_text` thuộc `QUARANTINE_STALE` có sẵn trong `tests/conftest.py`).

---

## 4. Cổng & Quy trình Canary Thực tế Có Giới hạn

- Module triển khai: `backend/cloud/cloud_canary.py`
  - `verify_canary_readiness(...)`: chặn tuyệt đối nếu `user_approved` chưa là `True`, `max_cu_cap <= 0`, còn bản ghi trong `list_pending_cleanup_events()`, còn ledger chưa đối soát (`unreconciled_count > 0`), ngân sách ngày hoặc số dư Colab tươi nhỏ hơn `max_cu_cap`, hoặc bất kỳ file nào trong 3 kịch bản (`short_video`, `special_name_video` bắt buộc có khoảng trắng/Unicode/ký tự đặc biệt, `cancel_case`) không vượt qua kiểm tra `0 CU`.
  - `compare_local_vs_cloud_deliverables(...)` & `render_canary_report_markdown(...)`: đối chiếu đầu ra Local và Cloud trên cùng video nguồn theo đủ 6 tiêu chí (`cue_completeness`, `voice_role_parity`, `subtitle_integrity`, `timing_parity`, `video_quality`, `cost_and_cleanup`), đặt `generalizable_accuracy_claim=False` (không suy diễn kết quả của một video mẫu thành tỷ lệ chính xác tổng quát hay áp rule riêng cho mọi video), và khử mọi bí mật trước khi xuất báo cáo Markdown.
- **Điều kiện để chuyển sang chạy Canary GPU thật**:
  - Người dùng cần xác nhận và cung cấp:
    1. Đường dẫn **video ngắn** (10–30 giây) được phép tải lên Cloud kèm kết quả Local đối chứng.
    2. Đường dẫn **video có tên đặc biệt** (chứa dấu cách, Unicode, dấu nháy/ký tự `&`).
    3. Đường dẫn **video thử hủy giữa chừng** (`cancel_case`).
    4. **Hạn mức trần chi phí (`max_cu_cap`)** và loại GPU muốn chạy thử (mặc định giữ `V1_CLOUD_MODE = False` cho tới khi xác nhận).

## 5. Tiếp nhận và Canary thực tế — đêm 14/11 sang 15/11/2026

Người dùng cung cấp ba link Xiaohongshu và yêu cầu tải về để test, sau đó ủy quyền
chọn GPU/ngân sách. Đợt này chọn **T4, tổng trần 5 CU**, dừng ở mức giảm số dư
4,5 CU để chừa dự phòng thu hồi. Hạn mức đợt được theo dõi từ số dư ban đầu
198,56 CU, xuyên qua nửa đêm; không chỉ dựa vào ngân sách theo ngày. Số dư nhà
cung cấp có độ trễ nên ngưỡng dừng phía ứng dụng không phải hard cap của provider.

| Ca | Link đã được người dùng chỉ định | Thời lượng | Kiểm tra tệp tải về |
|---|---|---:|---|
| Video ngắn | Mẫu Xiaohongshu 1 | 27,212336 s | SHA256 + ffprobe + giải mã đầy đủ hình/âm thanh đạt |
| Tên Unicode | Mẫu Xiaohongshu 2 | 32,633333 s | SHA256 + ffprobe + giải mã đầy đủ hình/âm thanh đạt |
| Hủy | Mẫu Xiaohongshu 3 | 59,833333 s | SHA256 + ffprobe + giải mã đầy đủ hình/âm thanh đạt |

- Đầu vào và bằng chứng chạy thật lưu trong `scratch/canary_inputs_20261114/`
  và `scratch/canary_run_20261114/` (đều bị Git bỏ qua).
- Đã xóa 60 tệp kết quả test cũ (14.544 byte); thêm quy tắc bỏ qua
  `backend/cloud/results/`, SQLite và các tệp journal/WAL/SHM. Sửa test đóng gói
  giai đoạn 4 để ghi kết quả vào `tmp_path`.
- Chạy lại 18 bộ test bàn giao trước sửa lỗi thực tế: **188 passed, 1 skipped**
  (20,16 s). Skip là quarantine sẵn có. Có hai cảnh báo deprecation và một cảnh
  báo coroutine chưa await trong test Telegram.
- Bản Local đối chứng video ngắn: **success, QC PASSED, 15 pass / 0 warning /
  0 failure**, 8 câu, CapCut `BV562_streaming`, 720×1280, 30 fps. Thành phẩm:
  `scratch/canary_run_20261114/local_baseline/deliverables/local_short.mp4`.
- Ca hủy thật `cloud-20261114-234810-979f94`: hủy sau upload bundle, trước upload
  video; trạng thái `cancelled`, cleanup `verified`, ledger còn nguyên. Tra cứu
  provider sau ca trả không còn session/assignment.
- Ca ngắn lần đầu `cloud-20261114-234827-e4e9f3`: thất bại
  `ENV_PACKAGE_MISSING`; Colab Python 3.13.16 không build được
  `openai-whisper==20240930`. Session đã thu hồi; số dư sau ca 198,45 CU
  (giảm 0,11 CU tại thời điểm đo). Ledger vẫn ghi chi phí ước tính theo runtime,
  không gắn nhãn số này là billing thực tế đã chốt.
- Lỗi thực tế đã sửa: metadata ffprobe Unicode đọc bằng UTF-8; parser Colab lấy
  đúng alias trong `[alias] provider-runtime-id` để đối soát cleanup; bootstrap
  Python 3.12.12 riêng trong `/content/.autodub_venv`, Torch/Torchaudio 2.5.1,
  Torchvision 0.20.1 CUDA 12.1, build tools cố định cho Whisper. Pipeline chạy
  bằng cùng interpreter đã tiền kiểm. CapCut TTS vẫn bắt buộc; không fallback Edge.
- Ca ngắn tiếp theo `cloud-20261114-235930-04a919`: tiền kiểm thất bại do
  NumPy ABI/cache sau cài đặt. Đã chuyển xác minh package sang subprocess mới,
  tránh dùng module native đã import trong tiến trình cài đặt; không lặp cài lại
  trong chế độ `--verify-installed`.
- Ca ngắn `cloud-20261115-000547-6e67a9`: provider CLI báo `Connection was lost`
  trong setup; job kết thúc `ENV_SETUP_FAILED`, session được thu hồi. Đã bổ sung
  phân loại chuỗi lỗi này là `ENV_NETWORK_ERROR` cho các lần chạy sau.
- Ca Unicode `cloud-20261115-003356-823338`: **môi trường GPU đã xác minh đạt**;
  pipeline chạy dịch, CapCut TTS và render, sau đó bị chặn `QC_FAILED`.
  Video nguồn dài **32,633333 s** nhưng ASR sinh 17 cue với mốc cuối **59,98 s**;
  logic cũ lấy mốc ASR làm độ dài nguồn, dẫn tới QC so đầu ra 32,63 s với 59,98 s.
  OCR không tìm được hộp phụ đề cho 17 cue. Dịch/TTS đủ 17 cue không chứng minh
  bản nhận dạng đúng. Không tải/xuất thành phẩm Cloud như một kết quả được chấp nhận.
- Đã thêm `validate_asr_source_timeline`: đo nguồn bằng ffprobe, chặn mốc không
  hợp lệ hoặc vượt nguồn với `ASR_TIMING_INVALID` **trước OCR/dịch/TTS**; không
  âm thầm bỏ hay rút ngắn cue. OCR dùng độ dài nguồn đã đo, không kéo dài theo ASR.
  Log TTS phân biệt số cue đã xác minh với số cue phải chỉnh tốc độ.
  Đây là chặn lỗi sớm, **chưa sửa nguyên nhân ASR sai và chưa chạy lại trên GPU**.
- Hồi quy cuối trên đủ 18 bộ test bàn giao: **193 passed, 1 skipped, 3 warnings
  trong 22,58 s**. Skip quarantine có sẵn; cảnh báo gồm FastAPI/httpx,
  pydub/audioop và coroutine mock Telegram chưa await. Test mới tái hiện việc
  cue kết thúc ở 59,98 s trên nguồn 32,633333 s và xác nhận chặn trước xử lý tiếp.

### Đối soát tại cuối đợt đầu tiên

- Bằng chứng: `scratch/canary_run_20261114/final_audit.json`, `run_summary.json`,
  `canary.log` và DB riêng `canary_jobs.db`; không đưa vào Git. `final_audit.json`
  hiện đã được cập nhật theo lần đối soát mới nhất ở mục 8; các số dưới đây
  là ghi nhận lịch sử cuối đợt đầu tiên.
- Số dư đầu **198,56 CU**, số dư cuối quan sát **197,82 CU**: giảm **0,74 CU**,
  dưới trần đợt 5 CU. Provider có thể cập nhật billing trễ; đây là chênh lệch
  số dư tại lần kiểm tra cuối, không phải hóa đơn đã chốt.
- Ledger giữ đủ 5 job (4 failed, 1 cancelled), tổng **ước tính runtime 0,788 CU**;
  không coi ước tính này là chi phí thực tế. Tất cả có `cleanup_verified=1`.
- Provider trả `sessions=[]`, `active_assignments=0`, `usage_rate=0.00/hr`;
  DB không có active job hoặc pending cleanup.
- SHA256 cả ba đầu vào không đổi sau xử lý. Ca hủy kiểm chứng lúc sau upload
  bundle/trước upload video; chưa phải bằng chứng hủy giữa render trên GPU thật.

Mặc định `V1_CLOUD_MODE=False` thực tế nằm trong `backend/v1_feature_flags.py`;
`cloud_config.py` không khai báo cờ này. Đợt thử dùng DB/runner riêng, không bật
worker hay chuyển hàng đợi toàn cục sang Cloud. Chưa commit hoặc stage dữ liệu.

## 6. Kết luận sáu tiêu chí ở đợt đầu tiên

Không có thành phẩm Cloud video ngắn hoàn tất để gọi bộ đối chiếu Local–Cloud
trên cùng nguồn. Vì vậy không tạo biên bản PASS giả từ dữ liệu thiếu.

| Tiêu chí | Kết luận đợt thử |
|---|---|
| `cue_completeness` | Chưa chứng nhận; thiếu đầu ra Cloud đối chứng, ASR Unicode vượt nguồn |
| `voice_role_parity` | Chưa đối chiếu được trên cùng nguồn |
| `subtitle_integrity` | Chưa đạt nghiệm thu; ca Unicode không tìm được hộp OCR, thiếu thành phẩm đạt QC |
| `timing_parity` | Không đạt ca Unicode: ASR 59,98 s trên nguồn 32,63 s |
| `video_quality` | Local đạt 15 kiểm tra; Cloud chưa có thành phẩm được chấp nhận |
| `cost_and_cleanup` | Đạt trong phạm vi 5 job đã chạy: dưới trần, ledger giữ đủ, thu hồi được xác minh |

**Canary Cloud CHƯA ĐẠT. Giai đoạn 8 bị chặn bởi P1 ASR timing.** Dừng đợt thử
trả phí tại đây. Việc tiếp theo là điều tra nguồn timestamp ASR sai trên ca
Unicode, kiểm chứng nội dung/cue và hộp OCR, rồi chạy lại video ngắn cùng ca
Unicode trên GPU. Chỉ khi có thành phẩm đạt QC và đủ sáu tiêu chí mới xem xét
Giai đoạn 8. Không suy diễn kết quả mẫu thành tỷ lệ chính xác tổng quát.

## 7. Cập nhật sửa nguyên nhân ASR — 15/11/2026

- Lỗi timestamp token trỏ vào phần đệm của cửa sổ Whisper 30 giây đã được sửa: giới hạn biên theo frame mel thật, căn chỉnh từng từ và chặn từ dồn về 0 giây.
- Bổ sung Silero VAD 6.2.0 trên audio gốc và vocal, hợp vùng lời theo mốc tuyệt đối, retry audio gốc một lần khi vocal bỏ trống vùng lời, rồi fail-closed bằng `ASR_TIMING_INVALID` nếu vẫn thiếu. Không tự cắt hoặc bỏ cue.
- T4 xác nhận Unicode chạy full pipeline đạt QC kỹ thuật (16 cue, 16 bản dịch/TTS, 32,633333 s); video ngắn có 17 cue. Mẫu dài bị chặn vì còn vùng lời bị bỏ trống sau retry, đúng chính sách bảo toàn nội dung.
- Hồi quy cuối: **202 passed, 1 skipped, 3 warnings**. Đối soát cuối: **1,98 CU** giảm theo số dư (ledger ước tính runtime 2,1888 CU), không còn session/assignment/pending cleanup, ba SHA256 nguồn không đổi.
- Chi tiết nguyên nhân và audit: [CLOUD_ASR_ROOT_CAUSE_20261115.md](CLOUD_ASR_ROOT_CAUSE_20261115.md).

**Kết luận vẫn là CHƯA ĐẠT và chưa mở Giai đoạn 8.** Lỗi timestamp đã hết trên ca Unicode, nhưng cần xử lý thiếu lời ở mẫu dài và OCR không định vị hộp chữ trước khi đánh giá lại sáu tiêu chí.

## 8. Sửa tiếp ASR và OCR — 15/11/2026

Cloud chuyển model sang `large-v3-turbo`, ưu tiên audio gốc như Local V1,
`beam_size=5`, `temperature=0`. Khi thiếu vùng lời, `avg_logprob < -1,0`
hoặc căn chỉnh không hợp lệ, thử lại vocal một lần với cùng các kiểm tra.
Nếu vẫn lỗi, dừng trước SRT/dịch/TTS. VAD, giới hạn frame mel thật và kiểm
tra timestamp theo nguồn vẫn được giữ. Không thêm mốc/câu đặc thù mẫu.
VAD dùng cùng model ONNX trong `faster-whisper==1.2.1` và chính sách Local
V1 (threshold 0,4, silence 600 ms, pad 400 ms); thay JIT 6.2.0 đã tạo vùng
lời giả ở nhạc cuối. Không chỉ nới kiểm tra độ tin cậy để nhận câu sai.

OCR trước đây tạo block với `in_subtitle_band=False` trước khi phân loại,
khiến selector loại mọi block. Đã bỏ cờ sớm này. Bản trung gian chạy thật
định vị được 12/12 hộp ở video ngắn, 25/26 ở video dài, 15/15 ở Unicode;
Unicode có vùng phụ đề trên `y=0,1898`, đã xem ảnh đối chiếu nguồn/đầu ra.

Job `cloud-20261115-110810-a576d0` đã chạy đủ ba video qua dịch, CapCut TTS
và render; tất cả đạt kiểm tra checksum và giải mã toàn bộ. Đây là bằng
chứng trung gian: kiểm tra nội dung phát hiện câu cuối mẫu dài do temperature
fallback sinh ngẫu nhiên, nên bản này không được dùng để chứng nhận ASR.
Đã chạy các phép đối chứng tiếp để chốt decoder và track nói trên.

Hồi quy bản sửa hiện tại: **207 passed, 1 skipped, 3 warnings (31,63 s)**
trên đủ 18 bộ Cloud + Local. Skip và warnings có sẵn. Chi tiết nguyên nhân,
thử nghiệm bị loại và audit nằm trong
[CLOUD_ASR_ROOT_CAUSE_20261115.md](CLOUD_ASR_ROOT_CAUSE_20261115.md).

### 8.1. Chạy lại hoàn chỉnh với bản sửa cuối

Job `cloud-20261115-122853-212130`, phiên T4 `cjob53212130a1`, hoàn tất
ba mẫu trong cùng phiên có ledger: `completed`, cleanup `verified`.
Decoder vẫn là `openai-whisper==20240930`; faster-whisper 1.2.1 cung cấp
bộ dò ONNX. Tất cả dùng audio gốc, không cần retry vocal.

| Mẫu | Nguồn | Đầu ra | Cue ASR / CapCut TTS đã xác minh | Mốc ASR cuối | OCR |
|---|---:|---:|---:|---:|---:|
| Video ngắn | 27,212336 s | 27,200000 s | 14 / 14 | 26,20 s | 14 / 14 |
| Video dài (nguồn ca hủy) | 59,833333 s | 59,833333 s | 20 / 20 | 58,24 s | 20 / 20 |
| Tên Unicode | 32,633333 s | 32,633333 s | 15 / 15 | 27,52 s | 15 / 15 |

Checksum/kích thước tải về khớp metadata từ GPU; giải mã toàn bộ ba video
bằng FFmpeg thành công. Audit không có gap lời quá 3 giây, mốc ngoài nguồn,
lỗi căn chỉnh hoặc cue dưới ngưỡng độ tin cậy. Số bản dịch/TTS bằng số cue;
SRT tiếng Việt không sót CJK. Đã xem ảnh nguồn/đầu ra của cả ba mẫu: OCR
Unicode dùng vùng trên `y=0,1805`; video dài có lời dịch/phụ đề ở giây 50/57.
Chưa dùng các kiểm tra kỹ thuật này để khẳng định đúng mọi chữ nhận dạng.

Thành phẩm và bằng chứng ở
`scratch/canary_run_20261114/asr_fix/cjob53212130a1/` (Git-ignored):
`{short,long,unicode}/output_video.mp4`, SRT, ASS, metadata, audit ASR,
`review.json`, `parity.json`, `parity.md` và `visual_review/`.
Thành phẩm Unicode do orchestrator tải về còn nằm tại
`scratch/canary_run_20261114/results/cloud-20261115-122853-212130/attempt_1/`.

### 8.2. Đối chiếu chính thức sáu tiêu chí — CHƯA ĐẠT

Đã gọi `compare_local_vs_cloud_deliverables` và xuất `parity.json`/`parity.md`
cho cùng nguồn video ngắn. Local đối chứng vẫn là bản 8 cue đã đạt 15 kiểm
tra QC; Cloud hiện có 14 cue.

| Tiêu chí | Kết quả bộ so sánh | Bằng chứng và giới hạn |
|---|---|---|
| `cue_completeness` | FAIL | Local 8 cue, Cloud 14 cue; ID 9–14 bị báo thừa, không có bản dịch rỗng hoặc sót CJK |
| `voice_role_parity` | PASS | Không mismatch trong 8 ID đối chiếu; chỉ là mẫu một giọng, không chứng nhận dual-voice |
| `subtitle_integrity` | PASS | 14 cue có khoảng thời gian hợp lệ |
| `timing_parity` | FAIL | Max drift theo ID 9,152 s, vượt ngưỡng 0,5 s; thời lượng hai thành phẩm bằng nhau |
| `video_quality` | PASS | QC đạt, h264/aac, độ phân giải khớp Local |
| `cost_and_cleanup` | PASS | Job cuối ước tính runtime 0,27 CU, ledger reconciled, cleanup verified; đối soát toàn đợt ở mục 8.3 |

Số cue khác nhau phản ánh khác cách chia câu. Ghép cùng ID khi câu đã chia
khác nhau không đủ để kết luận Cloud thừa sáu câu về nội dung hoặc lệch
âm/hình thực tế 9,152 s. Giữ nguyên hai FAIL; cần căn theo nội dung và thời
gian của các câu tương ứng để nghiệm thu, không ép số cue hay nới ngưỡng.

Tại mốc kiểm tra GPU này, caption khoảng 30,4 s còn cần nghe: cả Cloud
và phép đối chứng trực tiếp bằng Local Faster-Whisper đều không có cue riêng
ở đó. Chưa xác nhận caption này có trùng lời nói hay không. Thử thêm ngữ
cảnh có nhận một câu ở đây nhưng tái sinh câu giả cuối nên đã loại. Chi tiết
trong `review.json` và `faster_runtime_comparison.json`; chưa khẳng định
ASR đầy đủ từng chữ hoặc thay runtime sẽ giải quyết điểm này.

Đối chiếu tiếp ở mục 9 đã nhận xác nhận nghe của người dùng và khép nghi
vấn thiếu lời tại 30,4 s; ghi nhận trên được giữ như lịch sử kiểm tra GPU.

### 8.3. Chi phí, thu hồi và trạng thái bàn giao

- Số dư đầu **198,56 CU**, cuối **195,30 CU**: giảm **3,26 CU** cho toàn đợt,
  dưới trần 5 CU. Đây là chênh lệch số dư quan sát, có thể cập nhật billing trễ.
- Tổng ledger **3,7044 CU ước tính runtime**, khác phép đo số dư. Mọi job,
  gồm failed/cancelled, vẫn còn ledger và đều `cleanup_verified=1`.
- `sessions=[]`, `active_assignments=0`, `usage_rate=0`; không active job
  hoặc pending cleanup. Bằng chứng: `scratch/canary_run_20261114/final_audit.json`.
- SHA256 cả ba đầu vào không đổi; dữ liệu thử nghiệm vẫn bị Git bỏ qua.
  `V1_CLOUD_MODE=False`, CapCut TTS bắt buộc; chưa stage hoặc commit.
- Hồi quy cuối trên 18 bộ: **207 passed, 1 skipped, 3 warnings (31,63 s)**.
  Skip quarantine và warnings có sẵn; các cập nhật báo cáo sau đó không đổi code.

**Các lỗi ASR nghiêm trọng đã tái hiện được sửa và kiểm chứng trên ba mẫu;
Canary tổng thể vẫn CHƯA ĐẠT.** Giữ Giai đoạn 8 đóng tới khi hoàn tất đối
chiếu nội dung/timing. Ca hủy thật vẫn chỉ chứng minh hủy sau upload bundle,
trước upload video; lần chạy đủ video dài này không phải bằng chứng hủy
giữa render. Không suy diễn ba mẫu thành độ chính xác chung cho mọi video.

## 9. Đối chiếu nội dung, timing và xác nhận nghe — 15/11/2026

Đã hoàn tất lượt kiểm tra cục bộ trên artifact hiện có, không tạo GPU mới.
Chi tiết, bảng ghép câu, bằng chứng và phần cần sửa nằm trong
[CLOUD_CONTENT_TIMING_REVIEW_20261115.md](CLOUD_CONTENT_TIMING_REVIEW_20261115.md).

- **Khác cách chia câu đã được giải thích:** 8 cue Local và 14 cue Cloud
  ghép bằng tiếng Trung trước dịch thành bảy nhóm liên tiếp. Không dùng
  timestamp chọn cặp. Max drift ranh giới ngoài nhóm **0,201 s**, trong
  ngưỡng cũ 0,5 s; không chứng nhận timing từng từ/TTS từ số đo này.
- **Đoạn 30,4 s đã khép nghi vấn:** người dùng nghe clip gốc 28,5–34 s và
  trả lời “Không có, chỉ nhạc/tiếng động” về cụm lời riêng trước `完美`.
  Không tính là thiếu cue. Receipt giữ nguyên trả lời và hash clip/nguồn;
  đây là xác nhận của người dùng, agent không nghe trực tiếp được.
- **Nội dung chưa đạt:** Cloud đổi `裁剪` (cắt) thành `材质` (chất liệu)
  tại 9,519–10,820 s, còn chữ `大` → `塌` và diễn giải giá `个位数` thành
  “vài nghìn” chưa có căn cứ. Đã xem frame nguồn. Baseline Local cũng sai
  đoạn đầu, nên không coi khớp Local là chuẩn đúng tuyệt đối.
- **P1 timing/che phụ đề thực:** cue dài đầu 0–27,46 s nhưng ASS đã hiện
  hết chữ Việt/hộp che ở 19,89 s; không event tới 31,22 s. Frame Cloud
  giây 25 lộ nguyên chữ Trung. Hàm sync thay end nguồn bằng độ dài TTS rồi
  kẹp tracking; một cue chứa nhiều câu không có mốc con để giữ nội dung
  theo hình. “OCR 20/20 hộp” chưa chứng minh che đủ suốt thời gian nguồn.

Giữ `parity.json`/`parity.md` chính thức cũ để bảo toàn bằng chứng; các báo
cáo mới không đổi FAIL thành PASS. Bốn kiểm chứng của bộ ghép và các
assertion audit ASS đã đạt, chỉ chứng minh phép đo/tái hiện lỗi, không
chứng nhận pipeline. Bản pipeline vẫn là bản có hồi quy 207 passed trước đó.

**Giai đoạn 8 tiếp tục đóng vì lỗi nội dung và timing render đã xác minh.**
Trước khi mở cần sửa chia cue dài, thời gian che nguồn độc lập với TTS,
QC lịch ASS cuối và các sai khác nội dung; không thêm quy tắc đặc thù mẫu.

## 10. Nghiệm thu sau sửa toàn bộ lỗi đã xác minh — 16/11/2026

Session T4 `cjob07db8a2aa1`, job `cloud-20261115-232507-db8a2a`, completed;
ba pipeline returncode 0. Bản ngắn 14 câu/27,200 s, Unicode 15 câu/32,633 s,
dài 30 câu/59,833 s. Đủ **59/59 CapCut TTS**, không fallback. Chi tiết sửa
và review ở mục 7 của [báo cáo nội dung/timing](CLOUD_CONTENT_TIMING_REVIEW_20261115.md).

### 10.1. Sáu tiêu chí và phạm vi

Đã chạy `compare_reviewed_short.py` trên thành phẩm mới, gọi
`compare_local_vs_cloud_deliverables(..., matching_mode="source_alignment")`.
Kết quả trong `scratch/canary_run_20261114/asr_fix/cjob07db8a2aa1/parity_reviewed_short.json/.md`:

| Tiêu chí | Kết quả | Bằng chứng/phạm vi |
|---|---|---|
| Đủ lời/nội dung | PASS | 14 câu Cloud phủ nội dung nguồn được rà độc lập; 8 câu Local ghép thành bảy nhóm, không ép số cue |
| Vai giọng | PASS | Metadata voice khớp các nhóm; chỉ mẫu một giọng |
| Phụ đề | PASS | SRT/ASS khớp text, timeline chữ và mask đủ; đã xem hình các mốc lỗi |
| Timing | PASS | Ranh giới ngoài nhóm lệch tối đa 0,201 s < 0,5 s; ASS và TTS có audit riêng |
| Video | PASS | h264/aac, hash/size metadata khớp, decode toàn bộ và download QC đạt |
| Chi phí/thu hồi | PASS | Toàn đợt giảm 4,20/5 CU; cleanup verified, không session/assignment/active job/pending cleanup |

Reference nội dung từ caption nguồn đọc độc lập trước candidate; ranh giới
nhóm kế thừa Local. Baseline Local có lỗi đã biết, nên không dùng text Local
làm chân lý. Không tuyên bố parity render Local hay từng từ từ phép ghép
nhóm. Ba review report mỗi mẫu gắn SHA256 nguồn, output và cue/voice cuối;
thiếu hoặc lệch hash evidence vẫn FAIL. Các báo cáo FAIL cũ được giữ nguyên.

Rà toàn bộ 59 câu VI và các hình nguồn: đã khép sai cắt/chất liệu, giá tự
thêm “nghìn”, tên giấy và mất prefix `拉直`. ASS không còn hết sớm/lộ thoại
nguồn tại giây 25. TTS PCM tránh padding MP3, max fit thực 1,490526×,
dưới giới hạn 1,5×. Hash/decode/duration/biên của mọi file TTS đều đạt.

Người dùng nghe bốn clip output và trả lời **“Nghe rõ, giữ cách đọc hiện
tại”**. Receipt `scratch/content_fix_20261115/human_listening_final.json`
ghi đúng phạm vi; agent không nghe trực tiếp, hai ASR đọc lại mixed audio
chỉ là hỗ trợ. Xác nhận nguồn tại 30,4 s vẫn giữ “Không có, chỉ nhạc/tiếng
động”, không thêm lời. Không suy rộng ra mọi video hoặc mọi frame/từ chưa nghe.

### 10.2. Ca hủy, hồi quy và chi phí

Ca hủy thật `cloud-20261114-234810-979f94` / `cjob10979f94a1` đã nhận hủy
sau upload bundle, trước upload video; terminal cancelled sau **4,053 s**,
cleanup verified, không output, provider không còn session. Ledger giữ
0,0023 CU **ước tính runtime**. `success:false` của pipeline là hủy chủ đích,
không phải cleanup thất bại. Không gọi đây là hủy giữa render. Bằng chứng
`run_summary.json[0]`, `canary.log` và `canary_jobs.db` trong thư mục Canary.

Hồi quy 324 passed/1 skipped trước Canary; mock Telegram sửa sau đó và kiểm
riêng 1 passed khi nâng RuntimeWarning thành lỗi. 11 file runtime/config
trong `code_receipt_v17.json` khớp byte bản hiện tại theo
`final_code_verification.json`. `git diff --check` đạt; chưa stage/commit;
scratch, media, DB, log và results không đưa vào Git.

Số dư **198,56 → 194,36 CU**, giảm **4,20/5 CU** cho toàn bộ attempts.
Ledger cộng **4,3137 CU ước tính runtime**, ghi riêng, không gọi là hóa đơn.
`final_audit.json` xác nhận không session/assignment/active job/pending
cleanup, toàn bộ ledger cleanup verified và ba source hash không đổi.

### 10.3. Bàn giao Giai đoạn 8

**Giai đoạn 7 đã khép các lỗi chặn nghiệm thu đã xác minh.** Quyết định và
hash các bằng chứng lưu tại `scratch/content_fix_20261115/phase7_closure.json`.
Đủ điều kiện bắt đầu triển khai Giai đoạn 8; không có nghĩa Cloud đã được
bật phát hành hoặc Giai đoạn 8 đã hoàn tất. Giữ `V1_CLOUD_MODE=False`.

Phần tiếp theo theo kế hoạch: khóa admission bền vững/cho phép từng job,
concurrency=1 trên DB, theo dõi deadline/QC/ledger/cleanup và diễn tập
rollback theo job thuộc quyền quản lý. Không dùng endpoint stop toàn bộ
provider cho rollback theo phạm vi job. Chuẩn bị patch/runbook và kiểm thử
cô lập trước khi bật thử một job; không tự chuyển toàn bộ hàng đợi sang Cloud.
