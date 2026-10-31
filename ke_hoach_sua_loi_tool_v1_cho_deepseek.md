KẾ HOẠCH SỬA LỖI TOOL V1 — BÀN GIAO CHO DEEPSEEK

Nguồn: review ngày 31/10/2026 (full pytest 1069 pass / 8 fail / 80 skip; 60/60 video gần nhất OK). Repo: C:\tool v1 — nhánh tool-v1 — HEAD b8f61e6.

0. QUY TẮC BẮT BUỘC (đọc trước khi làm)
Không dừng / kill / restart bất kỳ tiến trình Tool V1 đang chạy (main.py :8088, telegram_bot, background_service, ops_watchdog, tool_control :8090) trừ khi Phase đó ghi rõ và người dùng đã xác nhận.
Không xoá, không đổi tên backend\venv, backend\venv_py312, backend\model_venv, backend\model_venv_separator, D:\workspace, D:\banve.
Đầu tiên xử lý 5 file đang sửa dở (xem Phase 0). Không được ghi đè mất thay đổi của người dùng.
Mỗi Phase = 1 nhánh con + 1 commit riêng, message dạng fix(v1): ... / refactor(v1): .... Không gộp nhiều Phase vào 1 commit.
Sau mỗi Phase phải chạy:
```powershell
cd "C:\tool v1"
.\backend\venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```
Không được để số test pass giảm so với baseline (1069). Không được "sửa" test bằng cách xoá assert hoặc skip bừa.
Không đổi hành vi pipeline (thứ tự 8 bước, giọng, mixer, style ASS, model dịch) nếu Phase không yêu cầu.
Tuyệt đối không ghi API key / token vào code, log, commit.
Nếu gặp điều không chắc chắn → dừng và hỏi, không tự đoán.
Phase 0 — Chốt trạng thái git (bắt buộc, 10 phút)

Hiện trạng: chưa commit: backend/job_tracker.py, backend/templates/dashboard.html, backend/templates/script_studio.html, backend/templates/workflow.html, tests/test_dashboard_hardening.py; untracked app.log.1, backend/app.log.1.

Việc làm:

git diff 5 file trên, tóm tắt nội dung cho người dùng, hỏi commit hay stash.
Thêm app.log.* và backend/app.log.* vào .gitignore.
Tạo tag mốc: git tag pre-deepseek-fix-20261031.

Nghiệm thu: git status -s sạch; tag tồn tại.

Phase 1 — Gỡ nốt Qwen3-ASR (ưu tiên cao nhất)

Vấn đề: commit a63cede đã bỏ qwen3_asr khỏi V1_ASR_MODEL choices (backend/v1_feature_flags.py:79), nhưng còn ~235 tham chiếu Qwen → 8 test fail:

tests/test_v1_asr_mutual_exclusion.py  (3 test)
tests/test_v1_qwen_asr.py              (2 test)
tests/test_v1_snapshot_and_asr_policy.py (2 test)
tests/test_v1_video_router.py::test_workspace_scoped_asr_feature_flag

Lỗi gốc: ValueError: Invalid V1_ASR_MODEL: qwen3_asr.

File còn tham chiếu (theo số lần): ai/v1_qwen_asr_adapter.py (44), v1_model_registry.py (29), ai/transcription.py (24), v1_feature_flags.py (13), main.py (11), v1_orchestrator.py (10), model_policy.py (10), rerender_video_dual_voice.py (10), model_runtime_worker.py (5), v1_video_router.py (4), + templates/JS dashboard.

Việc làm:

Grep toàn bộ qwen (case-insensitive) trong backend/ (trừ venv) và tests/, cả .html/.js.
Xoá adapter ai/v1_qwen_asr_adapter.py + mọi nhánh if asr_model == "qwen3_asr", entry trong model registry/policy, option trong dropdown dashboard.
Migration an toàn: khi đọc config/frozen snapshot/preset cũ có qwen3_asr → tự map sang whisper_sensevoice + log WARNING một lần (không raise). Áp dụng ở get_feature_flags, nơi load job_frozen_configs.json, presets.
Test:
Xoá tests/test_v1_qwen_asr.py.
Viết lại các test mutual-exclusion / snapshot / router dùng whisper_turbo ↔ whisper_sensevoice ↔ sensevoice_small thay cho qwen.
Thêm test mới: config cũ chứa qwen3_asr → được migrate thành whisper_sensevoice, không lỗi.
Không đụng tới test_qwen_real_inference.py nếu nó thuộc mục khác — kiểm tra trước, nếu chỉ test Qwen thì xoá luôn.

Nghiệm thu:

Select-String -Path backend\*.py,backend\ai\*.py -Pattern qwen → 0 kết quả (ngoại trừ dòng migration map).
Full pytest: 0 failed.
GET http://127.0.0.1:8088/api/status vẫn hoạt động (chỉ đọc, không restart).
Phase 2 — Làm rõ chính sách Quality Gate (QC)

Vấn đề: v1_feature_flags.py:29 mặc định V1_QC_POLICY = "BLOCK", nhưng log production luôn policy=REPORT_ONLY. ~40% job PASSED_WITH_WARNINGS vẫn xuất ra D:\banve.

Việc làm:

Truy ra nguồn ghi đè REPORT_ONLY (env .env, presets.json, job_frozen_configs.json, hardcode trong v1_orchestrator.py, dashboard setting...). Báo cáo đường đi cụ thể file:dòng.
Thống kê 50 file *.qc.json gần nhất: nhóm warning nào xuất hiện nhiều nhất (top 5) và ví dụ video.
Không tự đổi policy. Trình bày cho người dùng 3 lựa chọn:
Giữ REPORT_ONLY (hiện tại),
WARN (xuất video nhưng gắn tiền tố QC_WARN_ + báo Telegram),
BLOCK (chặn các lỗi nghiêm trọng: còn chữ CJK, thiếu audio, lệch độ dài > X%).
Sau khi người dùng chọn → sửa sao cho mặc định trong code = giá trị thực tế dùng, chỉ còn 1 nguồn sự thật. Hiển thị policy hiện hành trên dashboard.

Nghiệm thu: log policy= khớp giá trị trong v1_feature_flags.py/setting; có test cho đường đi override.

Phase 3 — OCR quá khắt khe với video không có phụ đề

Vấn đề: job telegram_1b229b8e... (30/10 08:06): OCR audit {'no_caption_evidence': 6, 'unresolved': 1} → cả job fail. Video gần như không có sub nhưng 1 cue "unresolved" làm fail-closed toàn job.

Việc làm:

Đọc backend/v1_ocr_geometry.py (validate_mask_geometry) + ocr_utils.py nhánh "No reliable Chinese subtitle band detected".
Quy tắc mới đề xuất:
Nếu ≥ 80% cue là no_caption_evidence và các cue unresolved không có bằng chứng CJK trong vùng sub → coi là video không có phụ đề: chuyển unresolved → no_caption_evidence, render không che, ghi lý do vào ocr_geometry_report.json.
Nếu cue unresolved có CJK thật trong vùng sub → vẫn fail-closed như cũ (KHÔNG được nới lỏng trường hợp này).
Thêm test cho cả 2 nhánh (dùng dữ liệu giả như test_verify_production_readiness.py::test_subtitle_free_video_with_scene_cjk_does_not_fail_ocr_geometry).

Nghiệm thu: test mới pass; toàn bộ test OCR cũ (test_ocr_fail_closed.py, test_v1_ocr_geometry_repair.py, test_ocr_geometry_audit.py) vẫn pass.

Phase 4 — Tự xếp hàng lại job Telegram fail do mất mạng

Vấn đề: 30/10 09:28 mạng rớt (getaddrinfo failed) → ~40 log Telegram V1 job ... failed, job không tự retry.

Việc làm:

Trong telegram_bot.py::_process_telegram_job: phân loại lỗi mạng tạm thời (httpx.ConnectError, ReadError, requests ConnectionError, getaddrinfo, timeout tải Douyin/XHS) vs lỗi thật (video hỏng, OCR fail-closed, CJK residual).
Lỗi mạng tạm thời → đưa lại hàng đợi với backoff 1m → 5m → 15m, tối đa 3 lần; lưu số lần retry bền vững (file JSON atomic, như durable queue hiện có).
Báo Telegram 1 tin "đang chờ mạng, sẽ thử lại" thay vì spam lỗi.
Test: mock lỗi ConnectError → job được requeue, không mark failed; lỗi thật → fail ngay.

Nghiệm thu: test mới pass; không trùng job (dùng job_id làm khoá idempotent).

Phase 5 — Dọn môi trường & requirements

Hiện trạng pip check của backend\venv:

sympy mất METADATA (torch, onnxruntime cần).
protobuf 7.35.1 xung đột google-ai-generativelanguage 0.6.15 & grpcio-status 1.71.2 (cần <6).
opencv-python-headless 5.0.0.93 đòi numpy>=2 (đang 1.26.4).
Gói thừa: rvc-python, fairseq, qwen-asr, qwen-omni-utils.
backend\venv_py312: pip check sạch.

Việc làm (CHỈ thực hiện trên venv_py312, KHÔNG sửa backend\venv đang chạy):

Đồng bộ requirements.txt với import thực tế (quét import trong backend, đối chiếu). Pin: numpy>=1.23.5,<2, protobuf<6, opencv-python-headless<5 (hoặc bản tương thích numpy 1.x).
Không thêm lại rvc/fairseq/qwen.
Tạo requirements.lock.txt = pip freeze của venv_py312 sau khi pip check sạch.
Chạy full pytest bằng venv_py312 → phải ≥ kết quả của venv 3.10.
Viết script scripts/cutover_py312.bat + scripts/rollback_py310.bat (đổi tên thư mục, KHÔNG xoá) nhưng không tự chạy — để người dùng chạy sau khi đã TatHoanToan.bat.

Nghiệm thu: venv_py312\Scripts\python.exe -m pip check → "No broken requirements"; full pytest trên 3.12 = 0 failed; v1_preflight.py --interface all → ready: true.

Phase 6 — Giảm nuốt lỗi im lặng (except: pass)

Hiện trạng: 401 khối except ...: pass, 804 except Exception, 4 bare except:. Nhiều nhất: workflow_api.py (43), main.py (27), tool_control.py (24), video_utils.py (21), segment_editor_service.py (18), translation.py (17), job_tracker.py (13), batch_processor.py (12).

Việc làm:

Sửa 4 bare except: → except Exception:.
Với 8 file trên: mỗi except ...: pass thay bằng logger.debug("...: %s", exc, exc_info=False) hoặc logger.warning nếu nằm trong luồng pipeline chính (translation, video_utils, job_tracker, batch_processor).
KHÔNG đổi luồng điều khiển (không raise thêm) — chỉ thêm log. Ngoại lệ: chỗ nào nuốt lỗi khiến kết quả sai âm thầm thì liệt kê ra để người dùng quyết định.

Nghiệm thu: số except: pass trong 8 file giảm ≥ 80%; full pytest không đổi.

Phase 7 — Tách nhỏ hàm khổng lồ (refactor không đổi hành vi)

Mục tiêu (theo thứ tự):

Hàm	File	Dòng
execute_job	v1_orchestrator.py	903
generate_ass_file	ass_utils.py	652
perform_video_ocr	ocr_utils.py	566
process_single_local_video	batch_processor.py	538

Cách làm:

Trước khi tách: viết golden test cho execute_job — chạy pipeline với mock các stage nặng (ASR/OCR/TTS/render), ghi lại thứ tự stage, artifact sinh ra, nội dung translation.json/final.ass; so sánh trước/sau.
Tách execute_job thành các hàm _stage_download, _stage_separate, _stage_asr, _stage_translate, _stage_ocr, _stage_tts, _stage_mix, _stage_render, _stage_qc_and_deliver, mỗi hàm < 150 dòng, giữ nguyên checkpoint/resume.
Làm từng hàm một, mỗi hàm 1 commit.

Nghiệm thu: golden test giống hệt trước/sau; full pytest 0 failed; chạy thử 1 video thật ngắn (<30s) do người dùng bấm trên dashboard → output tương đương bản cũ.

Phase 8 — Việc nhỏ
Đổi @app.on_event("startup") (main.py:246) sang lifespan.
Đánh giá có cần giữ ngoại lệ X-Dashboard-Input cho 5 endpoint (/api/stop-batch, /api/pause-video, /api/resume-video, /api/audio-settings, /api/worker-settings) ở main.py:185-189; nếu dashboard đã gửi token thì bỏ ngoại lệ.
Gemini model gemini-3-flash-preview là bản preview: thêm cấu hình model fallback ổn định (đọc từ setting, không hardcode) và log rõ khi fallback.
Ghi log với encoding="utf-8" nhất quán.
Phase 9 — (Tuỳ chọn) Hiệu năng & RAM

Số liệu: trung vị thời gian xử lý = 5× độ dài video; Bước 2 (BS-RoFormer) ~60s, Bước 3 (ASR) ~50s; RAM hệ thống 93–94%.

Đo RSS từng tiến trình khi idle & khi chạy; tìm model nào giữ RAM khi idle (ASR/OCR worker có unload sau stage không).
Thử RoFormer fp16 / chunk lớn hơn trên RTX 4050 6GB, so sánh SDR/độ nghe & thời gian; chỉ bật nếu chất lượng không giảm. Để dưới cờ feature flag, mặc định TẮT.
Báo cáo cuối mỗi Phase (DeepSeek phải gửi)
Phase: X
Nhánh/commit: ...
File thay đổi: ...
Pytest: N passed / M failed / K skipped (trước: ...)
Điểm cần người dùng quyết định: ...
Rủi ro còn lại: ...
Thứ tự & ưu tiên
Ưu tiên	Phase	Ước lượng
Bắt buộc	0, 1	1–2 giờ
Cao	2, 3, 5	3–5 giờ
Trung bình	4, 6	3–4 giờ
Thấp	7, 8, 9	1–2 ngày
