# BÁO CÁO REVIEW & ĐÁNH GIÁ TOOL V1 (AutoDub Video Tools)

- **Đối tượng review:** `C:\tool v1` — nhánh `tool-v1`
- **HEAD khi đánh giá:** `026d0e6` — `fix(douyin): configure Douyin cookies, install gmssl/curl_cffi, protect credentials in gitignore and harden workspace cleaner` (2026-10-16 15:31 +07)
- **Ngày đánh giá:** 2026-10-17
- **Người/Nguồn thực hiện:** AI Code Review (OpenCode)
- **Trạng thái cây làm việc khi đánh giá:** ⚠️ **BẨN / WIP** — 15 file sửa + 1 file mới chưa commit (tính năng SenseVoice ASR hybrid). Cây làm việc còn bị chỉnh sửa *trực tiếp trong lúc đánh giá* (2 file `canvas_settings.py`, `workflow_api.py` xuất hiện thêm giữa phiên). Toàn bộ kết quả trong báo cáo phản ánh **snapshot tại thời điểm đánh giá**, không phải trạng thái commit cuối.
- **Phạm vi:** Kiến trúc, đúng đắn nghiệp vụ/fail-closed, ổn định & phục hồi, bảo mật API local, chất lượng test & CI, vận hành production thực tế, triển khai/tài liệu, vệ sinh repo.
- **Thang điểm:** 0–10 theo trọng số từng nhóm (bảng ở mục 1). Diễn giải: `<5 = chưa đạt`, `5–6.5 = cần siết`, `6.5–8 = khá/tốt`, `>8 = xuất sắc`.

> Mọi kết luận đều kèm bằng chứng `file:dòng` hoặc output lệnh. Các mục suy luận/ước lượng được ghi rõ.

---

## 1. TÓM TẮT ĐIỀU HÀNH (EXECUTIVE SUMMARY)

Tool V1 là hệ thống lồng tiếng & dịch video tự động 8 bước đang **vận hành production thật hàng ngày** (bằng chứng log ngày 17/10/2026: job hoàn tất trọn 8 bước, render NVENC 322s, giao video về `D:\banve`, QC `PASSED_WITH_WARNINGS`, cleanup 124.6 MB). Kiến trúc pipeline nghiêm túc và tiến bộ vượt bậc so với báo cáo review ngày 15/10/2026: hết 4 lỗi collection, giảm 37 test đỏ, phần lớn lỗ hổng bảo mật CRITICAL đã được bịt.

Tuy nhiên, tại thời điểm đánh giá, dự án **vẫn chưa đạt trạng thái "V1.0 Final"** theo chính tiêu chí của dự án (`master_plan.txt` Phase 7/8), vì 3 khoảng cách:

1. **Fail-closed chưa được thực thi ở đường chạy chính:** orchestrator chủ động gọi `strict_geometry=False` (OCR) và `strict=False` (dịch thuật); Quality Gate mặc định `REPORT_ONLY` và không chặn giao video.
2. **Bộ test còn đỏ 73/888** — đa số là test cũ chưa cập nhật sau refactor (test drift) và ô nhiễm chéo giữa các test, nhưng vẫn có test an toàn then chốt đỏ.
3. **Cây làm việc chưa sạch:** 16 file WIP chưa commit; rò rỉ key cũ trong `app.log` (9 dòng); `Tool_V1.0_Final_RC2.zip` chứa `backend/.env` nằm ngay thư mục gốc.

### Bảng điểm tổng

| # | Tiêu chí | Trọng số | Điểm /10 | Điểm có trọng số | Xếp loại |
|---|---|---|---|---|---|
| 1 | Kiến trúc & thiết kế | 15% | **8.0** | 1.20 | Khá/tốt |
| 2 | Đúng đắn nghiệp vụ (fail-closed) | 20% | **6.0** | 1.20 | Cần siết |
| 3 | Ổn định & phục hồi | 15% | **7.0** | 1.05 | Khá |
| 4 | Bảo mật | 10% | **6.0** | 0.60 | Cần siết |
| 5 | Chất lượng test & CI | 15% | **5.0** | 0.75 | Cần siết |
| 6 | Vận hành thực tế (production) | 10% | **8.0** | 0.80 | Tốt |
| 7 | Triển khai & tài liệu | 10% | **6.0** | 0.60 | Cần siết |
| 8 | Vệ sinh repo | 5% | **6.0** | 0.30 | Cần siết |
| | **TỔNG CỘNG** | **100%** | | **6.5 / 10** | **KHÁ — vận hành được, chưa đủ điều kiện đóng v1.0.0** |

**Nhận định một dòng:** *Sản phẩm nội bộ chất lượng khá, kiến trúc tốt hơn hẳn mặt bằng "tool cá nhân", vận hành ổn; điểm chặn lớn nhất để đóng v1.0.0 là fail-closed chưa thực thi, 73 test đỏ và vệ sinh secrets/WIP.*

---

## 2. PHẠM VI & PHƯƠNG PHÁP

### 2.1. Cách kiểm chứng

| Hoạt động | Lệnh / Cách làm | Kết quả |
|---|---|---|
| Thống kê mã nguồn | `Get-ChildItem` + đo dòng | 235 file `.py` backend (~63.900 dòng), 131 file test (~16.800 dòng) |
| Kiểm tra cú pháp | `python -m compileall -q backend tests` | PASS (exit 0) |
| Thu thập test | `pytest tests --co` | 887 test collect thành công, 0 lỗi collection |
| **Chạy full test suite (lần sạch)** | `pytest tests -q --tb=no -p no:cacheprovider --continue-on-collection-errors` | **73 failed, 811 passed, 4 skipped, 158 subtests passed — 231.72s** |
| Chạy lại 37 file test đỏ (tách ô nhiễm) | `pytest <danh sách file> -q` | 67 failed / 218 passed → ~6 test đỏ do ô nhiễm chéo, pass khi chạy riêng |
| Chạy riêng lẻ để phân loại | `pytest <file>::<test> -q --tb=...` | Xác minh 2 nhóm pass riêng (ocr_proxy, sensevoice_hybrid), nhiều nhóm fail thật (resource_control, speech_translation_repair, stability, video_router, voice_ocr_optimization...) |
| Kiểm tra bảo mật | Đọc `main.py`, `script_api.py`, `dashboard_monitor.py`, `tool_control.py` + grep pattern | Xác nhận các lỗ hổng cũ đã sửa/chưa sửa (chi tiết mục 4.4) |
| Kiểm tra rò rỉ secrets | Grep `generateContent?key=` trong log; đọc entry `.env` trong RC2 zip | 9 dòng lộ key còn trong 2 file log; RC2 zip chứa `backend/.env` |
| Kiểm tra git | `git ls-files`, `git check-ignore`, `git tag`, `git status` | `.env` không bị track; chưa có tag `v1.0.0` |
| Kiểm tra production | Đọc `app.log`/`backend/app.log` ngày 17/10 | Job thật chạy thành công end-to-end |
| Đối chiếu tài liệu | `README.md`, `docs/RC3_REVIEWED_STATUS.md`, `docs/V1_FINAL_VERIFICATION.md`, `master_plan.txt` | README claim 12/12 test đã kiểm chứng đúng |

### 2.2. Hạn chế của phương pháp (công bằng)

- Một phần test đỏ là **test giòn/cũ** chưa cập nhật theo refactor, không hẳn lỗi hành vi — đã phân loại ở mục 6 (có ghi rõ mục nào đã xác minh trực tiếp, mục nào là ước lượng).
- Một số test phụ thuộc môi trường (GPU/ffmpeg/mạng) có thể fail do môi trường — nhưng nhóm `test_ocr_fail_closed`, `test_stage6_acceptance_matrix`, `test_rc3_safety`, `test_v1_tts_translation_guard` fail vì **bất đồng code ↔ kỳ vọng test**, không phải thiếu tài nguyên.
- Không chạy GPU soak-test/benchmark thật (không có bằng chứng tái lập trong repo — chính `docs/RC3_REVIEWED_STATUS.md` cũng thừa nhận điều này).
- Cây làm việc WIP đang được sửa song song trong lúc đánh giá → một số kết quả có thể xê dịch sau khi commit.

---

## 3. SNAPSHOT SỐ LIỆU HỆ THỐNG

| Hạng mục | Số liệu / Bằng chứng |
|---|---|
| Quy mô backend | 235 file `.py`, ~63.900 dòng (không tính venv/`__pycache__`/`scripts_and_tests`) |
| Quy mô test | 131 file, ~16.800 dòng, **888 test** |
| File lớn nhất (monolith) | `main.py` 2.014 · `workflow_api.py` 1.574 · `dashboard_monitor.py` 1.537 · `tool_control.py` 1.453 · `telegram_bot.py` 1.302 · `v1_orchestrator.py` 931 (dòng) |
| Git | 514 file tracked; chỉ có 1 tag: `archive/tool-v2-legacy`; **chưa có `v1.0.0`** |
| Cây làm việc | 15 file `M` + 1 file `??` (`backend/ai/v1_sensevoice_adapter.py`) |
| Dung lượng project | 27.9 GB (chủ yếu `venv`, `model_venv`, `model_venv_separator`, `models/`) — workspace runtime chỉ còn 0.03 GB (đã dọn rất tốt) |
| Dịch vụ | Dashboard V1 `:8088`, Tool Control `:8090`, Telegram Bot; legacy `dashboard_monitor.py` `:8089` (V2 retired) |
| Chất lượng code tĩnh | 4 `except:` trần; 646 `except Exception` (backend + backend/ai); 88 test đọc source để assert |
| Khởi động | `KhoiDongAn.vbs` → `start_bot.bat` có chạy `v1_preflight.py` trước khi lên dịch vụ ✅ |

### Bằng chứng vận hành thật (log 17/10/2026)

```text
18:34  v1_auto_voice  Nhận diện thành công người nói đầu tiên: NAM (F0=174.0 Hz, conf=0.80) — 0.148s
18:34  pipeline.progress  Bước 6/8: Lồng tiếng AI CapCut (6 luồng)...
18:35  pipeline.progress  Bước 7/8: Trộn nhạc nền & giọng đọc (Hierarchical Adaptive Mixer)...
18:35  pipeline.progress  Bước 8/8: Render video thành phẩm bằng NVENC GPU...
18:40  video_utils  V1 render complete encoder=h264_nvenc seconds=322.89 bytes=130604340
18:40  v1_orchestrator  [QUALITY_GATE] ... (policy=REPORT_ONLY)
18:40  v1_quality_gate  Đã xuất báo cáo QC (PASSED_WITH_WARNINGS)
18:40  v1_orchestrator  [DELIVERY_GUARD] Đã tự động lưu video hoàn tất tới: D:\banve\Dubbed_...mp4
18:40  workspace_cleaner  [CLEANUP] Job ...: freed 124.6 MB
```

→ Hệ thống chạy thật, tự phục hồi, giao sản phẩm OK. Log cũng cho thấy lúc 23:04 một dòng `stage=asr_worker_total seconds=24.84 success=False` — cần theo dõi thêm trong giai đoạn WIP ASR.

---

## 4. ĐÁNH GIÁ CHI TIẾT THEO 8 TIÊU CHÍ

### 4.1. Kiến trúc & thiết kế — 8/10 ⭐ (điểm mạnh nhất)

**Đánh giá:** Kiến trúc pipeline chuyên nghiệp, vượt trội mặt bằng tool nội bộ. Trừ điểm chủ yếu vì monolith và dead code V2 còn sót.

**Điểm tốt (đã xác minh code):**
- Orchestrator 8 stage với `V1Orchestrator` (`v1_orchestrator.py`): timeout tính theo độ dài video + mode (`. _calculate_stage_timeout` :199-231), GPU guard bắt buộc CUDA trước stage AI (:184-197), checkpoint từng stage qua `ManifestManager`.
- **Smart resume chuẩn:** verify manifest + hash artifact, đủ điều kiện mới bỏ qua stage (`verify_manifest_resumability`, xử lý đặc biệt khi `visual_ocr`/`ai_tts` chưa xác thực :401-417).
- **Cô lập tiến trình đúng chuẩn:** ASR subprocess riêng (`ai/v1_asr_isolated.py` — có retry `STATUS_ACCESS_VIOLATION`), separator trong `backend/model_venv_separator` riêng, cấm fallback CPU khi CUDA fail (`ocr_utils.py:167-177` fail-closed cho PP-OCRv6).
- **GPU Gatekeeper** (`v1_gpu_gatekeeper.py`): mutex đa tiến trình + re-entrancy + chờ VRAM hạ nhiệt thay vì OOM; hoạt động thấy rõ trong log production (`>>> ĐÃ CẤP QUYỀN GPU cho stage: 'video_rendering'`).
- Phân tầng rõ: `backend/ai/` (23 module), routes theo phase A–D, service layer (checkpoint, recovery, preset, segment editor, render history...).
- Vệ sinh kỹ thuật: không `shell=True`, không `pickle.load`/`eval` dữ liệu người dùng, chỉ 4 `except:` trần.
- Kiểm soát rủi ro thực tế tốt: `workspace_cleaner`, `durable_jobs`, lock lịch sử render bằng OS lock, downloader atomic temp→validate→ffprobe→`os.replace`.

**Điểm trừ:**
- Monolith `main.py` 2.014 dòng; `workflow_api.py` 1.574; `dashboard_monitor.py` 1.537 — bảo trì tốn công, dễ phát sinh lỗi giao thoa.
- **Dead code V2** còn tồn: `dashboard_monitor.py` (cổng 8089, không auth) vẫn nằm trong danh sách process của `tool_control.py` (:57, :92, :217-229) dù V2 retired.
- 646 khối `except Exception` — từng nuốt lỗi thật (case `NameError` nhánh OpenAI/DeepSeek đã sửa, nhưng pattern vẫn phổ biến).

### 4.2. Đúng đắn nghiệp vụ / fail-closed — 6/10

**Đã sửa tốt kể từ review 15/10 (xác minh code):**

| Vấn đề cũ | Trạng thái hiện tại | Bằng chứng |
|---|---|---|
| TTS trả "thành công giả" (sinh `anullsrc`) | ✅ Đã xóa; lỗi giờ raise sau retry | `ai/voice_cloning.py:112-135` (nhánh `anullsrc` còn lại chỉ dành cho text không có từ đọc được — hợp lệ) |
| Cache audio im lặng | ✅ `is_valid_audio` chặn audio câm `dBFS < -75` | `ai/v1_voice_cache.py:46-48` |
| `_parse_json_array` / `_subdivide_and_retry` không tồn tại | ✅ Đã định nghĩa đầy đủ | `ai/translation.py:68, :107` |
| Phát hiện CJK quá hẹp (chỉ U+4E00–9FFF) | ✅ Đã mở rộng Extension A `U+3400–4DBF` | `v1_quality_gate.py:93`, `ai/v1_sensevoice_adapter.py:41-42` |
| `V1_ALLOW_TRANSLATION_FALLBACK` mặc định `true` | ✅ Mặc định nay là **false** | `ai/translation.py:945, :1004` |
| Path traversal `/api/script/stream` | ✅ Containment check | `script_api.py:99-118, :852-868` |
| CORS `Origin: null` | ✅ Đã lọc bỏ | `main.py:58` |
| So sánh token thường | ✅ `secrets.compare_digest` | `main.py:131` |
| Lộ key Gemini trong log | ✅ Mask bằng `mask_secret` | `ai/translation.py:415` |

**Còn hở (xác minh code — đây là lý do điểm 6 chứ không cao hơn):**

- 🔴 **Fail-closed bị tắt chủ động tại đường chạy chính:**
  - `v1_orchestrator.py:607` — `perform_video_ocr(..., strict_geometry=False)` → `OCRGeometryError` **không bao giờ được raise** trong pipeline thật.
  - `v1_orchestrator.py:671` — `translate_subtitles(..., strict=False)` → lỗi chất lượng dịch chỉ ghi warning ("Proceeding with sanitized translation to avoid job failure", `translation.py:949`) rồi đi tiếp.
- 🔴 **Quality Gate mặc định `REPORT_ONLY`** (`v1_feature_flags.py`, xác nhận trong log production `policy=REPORT_ONLY`); Delivery Guard chỉ verify file video decode được (`v1_orchestrator.py:953-958`), **không chặn giao video khi QC fail**.
- 🟠 **`validate_mask_geometry`** (`v1_ocr_geometry.py:110-183`): trạng thái `no_caption_evidence` không bao giờ bị coi là `invalid` (:155-160) → khi OCR không phát hiện dải phụ đề, validation im lặng bỏ qua; auto-heal gán tọa độ median và dán nhãn `'located'` (:127-154) — che theo heuristic nhưng không được đánh dấu là chưa xác minh.
- 🟠 Đường fail-open OCR "không tìm thấy dải phụ đề": `ocr_utils.py:695-704` đặt `best_block=None`, `no_caption_evidence` cho mọi cue → không vẽ hộp che → nếu video **có** phụ đề nhưng OCR nhận diện hụt, chữ Trung gốc có thể lộ (chỉ QC báo cáo, không chặn).
- Test an toàn then chốt còn đỏ: `test_stage6_acceptance_matrix::test_release_gate_zero_exposed_chinese_text` (một phần do style ASS đổi tên `CoverStyle` → `BgStyle`), `test_v1_tts_translation_guard` (2 test), `test_ocr_fail_closed::test_both_telegram_ocr_handlers_raise`.

**Kết luận tiêu chí:** Các "mảnh ghép" fail-closed đã có (strict mode, quality gate BLOCK, delivery guard) nhưng **chưa được nối ở đường chạy chính** — hiện là thiết kế "ưu tiên hoàn thành job + báo cáo hậu kiểm". So với tiêu chí master_plan ("không render video sai khi OCR/TTS/translation lỗi") → **chưa đạt**.

### 4.3. Ổn định & phục hồi — 7/10

**Tốt (bằng chứng code + log production):**
- Checkpoint/manifest/resume; lock lịch sử render bằng OS lock; downloader atomic có timeout (không treo vô hạn — yêu cầu master_plan ✅).
- GPU gating + VRAM monitor + RAM-aware parallelism (log: `speech_parallelism=2 (RAM-aware cue admission)`).
- `recovery_service.py`, `batch_checkpoint.py`, `durable_jobs.py`, `failed_video_queue_monitor.py`, `workspace_cleaner` — hệ sinh thái phục hồi đầy đủ.
- Job thật ngày 17/10 hoàn tất end-to-end (render 322s, giao hàng, cleanup) — độ ổn định thực tế đã được chứng minh.
- TTS có retry + deadline (CapCut poll deadline, Edge TTS 4 attempts), dịch có circuit breaker + cooldown + multi-key failover.

**Trừ điểm:**
- `backend/server_crash.log` (27 KB) tồn tại — vết crash cũ chưa được xử lý gọn.
- Log ngày 17/10 có 1 dòng ASR worker `success=False` (`stage=asr_worker_total seconds=24.84`) đúng thời điểm WIP ASR.
- Nhiều lần bot khởi động liên tiếp 22:59→23:03 (4 lần trong 4 phút) — dấu hiệu restart churn cần theo dõi.
- Chưa có bằng chứng soak-test 10 video liên tục / crash-recovery test — chính `docs/V1_FINAL_VERIFICATION.md` và `RC3_REVIEWED_STATUS.md` trung thực thừa nhận "Real-media acceptance remains open".

### 4.4. Bảo mật — 6/10 (từ 2/10 → tiến bộ lớn)

**Đã bịt (xác minh code):**

| Lỗ hổng cũ (review 15/10) | Trạng thái | Bằng chứng |
|---|---|---|
| C1. Path traversal + đọc file tùy ý `/api/script/stream` | ✅ Đã fix (allowlist roots + `is_relative_to`) | `script_api.py:99-118, :852-868` |
| C2. CORS `Origin: null` + `/api/auth-token` | ✅ CORS lọc "null"; token không đọc được cross-origin | `main.py:52-64` |
| C3. Miễn trừ token nhiều endpoint ghi | 🟡 Thu hẹp còn `/api/script/*` + `/api/voice-preview`; dùng `compare_digest`; fallback dashboard nội bộ có kiểm tra Origin + custom header | `main.py:125-139` |
| C4. Dashboard :8089 không auth | 🟠 **Chưa fix** (legacy, không auto-start — rủi ro tiềm ẩn nếu ai đó khởi động) | `dashboard_monitor.py:121, :1686` |
| C5. Lộ key Gemini trong log | ✅ Code đã mask (`mask_secret`); ⚠️ **log cũ còn 9 dòng chứa key** | `ai/translation.py:415`; `app.log` + `backend/app.log` |
| C6. `_validate_safe_video_path` không giới hạn thư mục | ✅ Allowlist roots | `script_api.py:99-118` |
| Host/DNS rebinding | ✅ Middleware kiểm tra Host + IP client localhost | `main.py:110-123` |

**Tồn dư cần xử lý:**
- 🟠 **`/api/script/*` (20+ endpoint ghi) và `/api/voice-preview` miễn token** (`main.py:129`): rủi ro CSRF thấp–trung bình (phần lớn endpoint cần JSON body nên khó khai thác từ form cross-origin; vẫn nên bịt).
- 🟠 **`GET /api/auth-token` vẫn trả token không cần xác thực** (`main.py:980-983`) — đã giảm thiểu nhờ chặn CORS null (token cũng đã được inject trực tiếp vào HTML tại `main.py:975` nên endpoint này thừa).
- 🟠 `dashboard_monitor.py` (:8089) — `app = FastAPI()` không middleware nào (Host/token/IP), các endpoint `/api/run-batch`, `/api/stop-batch`, `/api/retry-video`, `/api/open-folder` mở hoàn toàn. Legacy nhưng chưa xóa khỏi repo/danh sách process.
- 🔴 **Secrets rò trong file log cũ:** 9 dòng `generateContent?key=...` trong `app.log` (25 MB) và `backend/app.log` (34 MB) → **cần rotate key + xóa log cũ**.
- 🔴 **`Tool_V1.0_Final_RC2.zip` (thư mục gốc) chứa `backend/.env`** — đã xác minh bằng đọc entry zip. Không bị commit git, nhưng tuyệt đối không được chia sẻ/gửi kèm.
- ⚪ `/api/logs` (GET) không cần token — local-only, rủi ro thấp.

**Điểm tốt giữ nguyên:** `.env` / `backend/.env` chưa từng bị commit (đã xác minh `git check-ignore` + `git ls-files`); Tool Control :8090 chuẩn (token `secrets.token_urlsafe`, `compare_digest`, Host check); packing script loại `.env*` và đối chiếu giá trị secret (`package_v1_release.py:91, :136`).

### 4.5. Chất lượng test & CI — 5/10

**Số liệu (đo trực tiếp):**
- **888 test: 73 failed, 811 passed, 4 skipped** → tỷ lệ pass **91.3%** (thời gian ~232s).
- So với 15/10: 110 failed / 750 passed / **4 collection errors** → nay **0 collection error**, +61 pass, −37 failed. Tiến bộ rõ rệt.

**Phân loại 73 test đỏ (chi tiết đầy đủ ở mục 6):**

| Nhóm nguyên nhân | Ước lượng | Đã xác minh trực tiếp |
|---|---|---|
| A. Test cũ chưa cập nhật sau refactor (test drift) | ~35–40 | `test_v1_voice_ocr_optimization` patch `_atomic_meta` không tồn tại; `test_v1_translation_final` gọi `job_tracker.record_translation_attempt` đã đổi tên; `test_v1_resource_control` đòi `video_id=` trong log đã đổi format; `test_stage6` đòi style `CoverStyle` (nay là `BgStyle`) |
| B. WIP mới (SenseVoice, video router) chưa ổn định | ~8–10 | `test_orchestrator_planning_and_gpu_guard` (SHORT vs LONG), `test_immutable_snapshot_in_frozen_config`, `test_sensevoice_hybrid` |
| C. Ô nhiễm chéo giữa test (pass khi chạy riêng) | ≥6 (đã xác minh) | `test_v1_ocr_proxy` (3 test pass riêng 100%: "3 passed in 0.08s"), `test_sensevoice_hybrid` (3 pass riêng: "3 passed in 6.66s") |
| D. Phụ thuộc môi trường (API key, ffmpeg) | ~10 | `GeminiConfigError` trong DispatcherTests; log `moov atom not found` khi test media |
| E. Test giòn đọc source/AST (chồng lấn A) | 88 vị trí | `test_ocr_fail_closed` quét source tìm handler (0 != 2); nhiều test hardcode `C:\tool v1` |

**Vấn đề hệ thống của bộ test:**
1. **Test không hermetic (ô nhiễm production):** `telegram_bot.py:78-86` cấu hình `logging.FileHandler` vào **log production ngay khi import** → trong lúc chạy test, `backend/app.log` bị ghi thêm dòng `=== AUTO VIDEO DUBBING TOOL V1 STARTED ===` với PID của tiến trình pytest (xác nhận qua log 23:03:39, PID 18608 = tiến trình pytest). Test cũng dùng chung thư mục `Temp\tmp...` và chạm vào workspace thật.
2. **Flakiness:** 2 lần chạy full suite cho kết quả 72 vs 73 failed (1 test dao động).
3. **CI quá yếu:** `.github/workflows/python-quality.yml` đã trigger đúng nhánh `tool-v1`/`main` (tiến bộ), nhưng **chỉ chạy 3 module unittest** (`test_app_paths`, `test_ocr_subtitle_locator`, `test_url_utils`) trên 131 file test — gần như không có giá trị gác cổng.
4. `conftest.py` có cơ chế đồng bộ module `backend.*` ↔ module trần (hack để hai kiểu import cùng trỏ một module) — che giấu vấn đề cấu trúc package, là gốc của một phần lỗi isolation.

**Điểm sáng:** Đã có test cho chính các fix gần đây (Gemini multikey pool, OCR geometry repair, speech translation repair, SenseVoice hybrid...). Bộ test đồ sộ (16.8k dòng) và nhiều test chất lượng thật (pixel cover QC 34KB, timeline & cover QC 39KB).

### 4.6. Vận hành thực tế (production) — 8/10 ⭐

**Tốt:**
- Vận hành thật hàng ngày qua Telegram Bot; quy trình khép kín: nhận link → tải → 8 bước → giao video `D:\banve` → dọn rác.
- Tự động hóa vận hành đầy đủ: `KhoiDongAn.vbs` (chạy ngầm), `TatHoanToan.bat` (tắt sạch), preflight check trước khi chạy, heartbeat/UI Tool Control 26 giọng, Studio Quy Trình/Kịch Bản.
- Auto-voice analysis tốt: nhận diện NAM/NỮ theo F0 conf 0.80 trong 0.148s, snapshot `voice_lock.json`, chống trùng giọng.
- QC report + render history + delivery guard hoạt động thấy rõ trong log.

**Trừ điểm:**
- Log production chứa ký tự **mojibake** ở một số dòng tiếng Việt (ví dụ `Th?i lu?ng video` trong `backend/app.log`) — gây khó tra cứu sự cố.
- Nhiều instance bot khởi động chồng lấn trong log (22:59, 23:03) — cần kiểm tra cơ chế single-instance (`bot_instance.lock`).
- Chưa có dashboard/alert khi job fail liên tục (dựa vào đọc log thủ công).

### 4.7. Triển khai & tài liệu — 6/10

**Tốt:**
- `setup_v1.ps1`, `setup_new_laptop.ps1`, `v1_preflight.py` (kiểm tra model/env trước khi chạy) — vận hành chuyên nghiệp.
- Tách venv chuẩn: `backend/venv` (app) vs `backend/model_venv*` (model nặng); model weights tải local, không đẩy git.
- Packing loại secrets + kiểm tra chéo giá trị secret (`package_v1_release.py`).
- README tuyên bố "12/12 passed" cho 2 file nghiệm thu — **đã kiểm chứng đúng** (`12 passed in 5.33s`).
- `docs/RC3_REVIEWED_STATUS.md` và `docs/V1_FINAL_VERIFICATION.md` **trung thực** (dám ghi "chưa benchmark, chưa soak, không claim speedup").

**Trừ điểm:**
- `requirements.txt` (root) pin `paddleocr>=2.7,<3` trong khi `backend/requirements-models.txt` pin `paddleocr==3.7.0` — xung đột nếu cài chung; danh sách còn thiếu vài dep trực tiếp (`soundfile`, `scipy`, `scikit-image`, `onnxruntime`, `google-genai`, `pydantic`, `safetensors`, `transformers`, `torch`).
- `docs/PROJECT_STATE.md` lệch thực tế (còn mô tả Tool V2 chạy :8089, danh sách giọng lẫn RVC).
- Model weights không nằm trong package và chưa thấy script tải model tự động rõ ràng.
- Chưa có `v1.0.0` tag; `RELEASE_METADATA.json` (nếu lấy từ V2 code-handoff) không phản ánh V1.

### 4.8. Vệ sinh repo — 6/10 (từ 3/10 → đã dọn rất nhiều)

**Đã dọn tốt:**
- `backups/` từ 181 file → 29 mục (13 dir + 16 file).
- `workspace/` runtime từ 40.3 GB / ~60k file → **0.03 GB**.
- `.gitignore` đầy đủ: `.env`, venv, model, `*.log`, workspace, temp.
- `.env`/`backend/.env` **chưa từng track** (đã xác minh).
- `docs/baseline`, `docs/archive` có phân loại; VERSIONS.md rõ ràng.

**Còn tồn:**
- Rác thư mục gốc: `test_no_watermark.mp4` (**68 MB**), `frame_bak_v8/v10.jpg`, `test_bak_v8/v10.mp4`, `test_capcut_*.mp3`, `out.srt` (0 byte), `app.log` (25 MB), **3 file zip release** (trong đó RC2 chứa `.env`!), `master_plan.txt` để ở root.
- `scratch/` 35 file debug; `backend/scripts_and_tests/` 61 file lịch sử; `.git.worktree_bak` trỏ sang `C:/tool v2` (không còn tồn tại).
- `backend/server_crash.log`, `backend/test_*.mp4/png/jpg` (media test nằm trong backend/).

---

## 5. SO SÁNH VỚI BÁO CÁO REVIEW 15/10/2026

| Hạng mục | 15/10 | 17/10 (nay) | Xu hướng |
|---|---|---|---|
| Test suite | 110 failed / 750 passed / **4 errors** | **73 failed / 811 passed / 0 errors** | 🟢 tốt lên rõ |
| Bảo mật C1–C6 | 6 lỗ hổng mức CRITICAL | C1, C2, C5, C6 fixed; C3 giảm thiểu; C4 tồn dư (legacy) | 🟢 tốt lên |
| Fail-closed OCR | `best_block=None` + không bao giờ raise | Vẫn còn đường fail-open; orchestrator gọi `strict_geometry=False` | 🟡 cải thiện một phần |
| Fail-closed TTS | Trả thành công giả + cache silence | ✅ Đã fix cả hai | 🟢 tốt lên |
| Fail-closed dịch | strict bị hạ cấp + fallback default true | Default `false`, raise khi strict; nhưng orchestrator vẫn `strict=False` | 🟡 cải thiện một phần |
| Hàm không tồn tại (`_parse_json_array`...) | Chết nhánh OpenAI/DeepSeek | ✅ Đã định nghĩa | 🟢 tốt lên |
| CJK detection | Chỉ Basic block | ✅ + Extension A | 🟢 tốt lên |
| CI | Chỉ main/tool-v2, không full suite | Phủ `tool-v1`, vẫn chỉ 3 module | 🟡 nhích nhẹ |
| Vệ sinh repo | 40 GB workspace, 181 backups | 0.03 GB, 29 backups | 🟢 tốt lên |
| Version tag | Chưa có v1.0.0 | **Vẫn chưa** | ⚪ giữ nguyên |
| Cây làm việc | Bẩn (19 file) | Bẩn (16 file, WIP SenseVoice) | ⚪ giữ nguyên |

---

## 6. DANH SÁCH ĐẦY ĐỦ 73 TEST ĐANG ĐỎ + PHÂN LOẠI

Tổng: **72 dòng `FAILED` + 1 `SUBFAILED`** (`test_guard_last_cue_and_speed`).
Nhóm A = test drift cần cập nhật; B = WIP mới; C = ô nhiễm chéo (đã xác minh pass khi chạy riêng); D = môi trường; ? = chưa phân loại chắc chắn.

| # | Test | File | Nhóm |
|---|---|---|---|
| 1 | `test_defaults_are_portable_and_outside_the_repository` | test_app_paths.py | ? |
| 2 | `test_failure_does_not_publish` | test_asr_isolated.py | B |
| 3 | `test_result_and_no_reuse` | test_asr_isolated.py | B |
| 4 | `test_other_io_errors_are_not_retried` | test_atomic_windows_retry.py | A |
| 5 | `test_permanent_denial_is_bounded_and_keeps_old_manifest` | test_atomic_windows_retry.py | A |
| 6 | `test_dashboard_html_served` | test_dashboard_and_tracker.py | A |
| 7 | `test_job_tracker_error_and_stop` | test_dashboard_and_tracker.py | A |
| 8 | `test_job_tracker_lifecycle` | test_dashboard_and_tracker.py | A |
| 9 | `test_template` | test_dashboard_monitor.py | A (legacy V2) |
| 10 | `test_all_translation_providers_receive_duration_budgets` | test_fast_profile.py | A |
| 11 | `test_gemini_failures_have_bounded_attempts_and_no_query_key` | test_final_acceptance.py | D/A |
| 12 | `test_vfr_proxy_retains_original_dimensions_and_gpu_encoder` | test_final_acceptance.py | D (media) |
| 13 | `test_render_fallback_to_libx264_on_nvenc_failure` | test_long_video_resource_optimization.py | D (ffmpeg) |
| 14 | `test_gemini_health_cache_and_retry_limits` | test_multi_llm_translation.py | A |
| 15 | `test_prompt_builder_with_glossary_entity_speaker_and_cross_batch_context` | test_multi_llm_translation.py | A |
| 16 | `test_translate_subtitles_forwards_glossary_and_maps` | test_multi_llm_translation.py | A |
| 17 | **`test_both_telegram_ocr_handlers_raise`** (an toàn) | test_ocr_fail_closed.py | A (quét source, 0 != 2) |
| 18 | `test_source_lifetime_outside_speech_is_covered_without_extending_text` | test_ocr_geometry_audit.py | ? |
| 19 | `test_truncated_tail_does_not_stretch_last_frame_across_gap` | test_ocr_runtime_audit.py | ? |
| 20 | `test_local_renderer_uses_portable_environment_paths` | test_pipeline_v2_preservation.py | A |
| 21 | `test_telegram_network_updates_use_safe_wrapper` | test_pipeline_v2_preservation.py | A |
| 22 | **`test_history_live_lock_cannot_be_stolen`** (an toàn) | test_rc3_safety.py | ? |
| 23 | `test_api_request_passes_selected_font_into_pipeline` | test_review_regressions.py | ? |
| 24 | `test_hybrid_refiner_4_layers_with_mocked_sv_analysis` | test_sensevoice_hybrid.py | **C — pass riêng (3 passed)** |
| 25 | `test_landscape_subtitle_width_not_clamped_to_65_percent` | test_stage2_subtitle_and_qc.py | ? |
| 26 | `test_ocr_timeout_handling` | test_stage4_full_chain_qc.py | ? |
| 27 | **`test_release_gate_zero_exposed_chinese_text`** (an toàn) | test_stage6_acceptance_matrix.py | A (style `CoverStyle`→`BgStyle`) |
| 28 | `test_end_to_end_queue_to_delivery_preserves_chat_id` | test_telegram_bot_v2_delivery.py | A |
| 29 | `test_whisper_model_cached_and_released` | test_top5_fixes.py | ? |
| 30 | `test_cmd_stop_graceful_sequence` | test_top5_fixes.py | ? |
| 31 | `test_all_failed_fail_fast_during_cooldown` | test_translation_optimization.py | A |
| 32 | `test_failed_model_skipped_next_video_and_recovers` | test_translation_optimization.py | A |
| 33 | `test_transport_failure_does_not_poison_next_job` | test_translation_optimization.py | A |
| 34 | `test_divide_and_conquer_on_gemini_length_mismatch` | test_translation_resilience.py | A |
| 35 | `test_divide_and_conquer_preserves_duration_budgets` | test_translation_resilience.py | A |
| 36 | `test_gemini_configurable_timeout` | test_translation_resilience.py | A |
| 37 | `test_ducking_preserves_late_audio_marker` | test_v1_audio_refactor.py | ? |
| 38 | `test_float_mix_does_not_flatten_overlapping_voice_peaks` | test_v1_audio_refactor.py | ? |
| 39 | `test_missing_voice_is_an_error` | test_v1_audio_refactor.py | ? |
| 40 | `test_separator_gpu_lock_concurrency` | test_v1_bs_roformer_audio.py | D (GPU) |
| 41 | `test_asr_reuse_and_audio_change` | test_v1_checkpoint.py | B |
| 42 | `test_11_fpt_key_isolation` | test_v1_gemini_stability_codex.py | D (key) |
| 43 | `test_resolve_effective_model_by_mode` | test_v1_model_registry.py | B |
| 44 | `test_smart_skip_ocr_filter_logic` | test_v1_model_registry.py | B |
| 45 | `test_cancel_does_not_start_original_after_proxy` | test_v1_ocr_proxy.py | **C — pass riêng** |
| 46 | `test_direct_vfr_input_passes_probed_timeline_duration` | test_v1_ocr_proxy.py | **C — pass riêng** |
| 47 | `test_large_source_preserves_original_dimensions` | test_v1_ocr_proxy.py | **C — pass riêng** |
| 48 | `test_stage_context_and_failure` | test_v1_resource_control.py | A (đòi `video_id=` trong log) |
| 49 | `test_worker_environment_correlation` | test_v1_resource_control.py | A |
| 50 | `test_auth_stops_chain` | test_v1_speech_translation_repair.py | D (`GeminiConfigError`) |
| 51 | `test_bad_payload_stops_chain` | test_v1_speech_translation_repair.py | D |
| 52 | `test_cancel_after_response` | test_v1_speech_translation_repair.py | D |
| 53 | `test_cancel_before_network` | test_v1_speech_translation_repair.py | D |
| 54 | `test_empty_200_tries_next` | test_v1_speech_translation_repair.py | D |
| 55 | `test_expired_deadline` | test_v1_speech_translation_repair.py | D |
| 56 | `test_failed_response_retains_cause` | test_v1_speech_translation_repair.py | D |
| 57 | `test_partial_200_tries_next_model` | test_v1_speech_translation_repair.py | D |
| 58 | `test_guard_measures_real_file_not_metadata` | test_v1_speech_translation_repair.py | A |
| 59 | `test_unresolved_overflow_is_rejected_without_cache` | test_v1_speech_translation_repair.py | A |
| 60 | `test_direct_refuses_silent_speech_clipping` | test_v1_speech_translation_repair.py | A |
| 61 | `test_hierarchy_refuses_global_overlap_before_slices` | test_v1_speech_translation_repair.py | A |
| 62 | `test_gemini_skips_recent_failed_model_and_keeps_full_array` | test_v1_stability.py | A (thứ tự model đổi) |
| 63 | `test_successful_cue_reused_but_changed_text_invalidates` | test_v1_stability.py | A |
| 64 | `test_old_video_cannot_record_model` | test_v1_translation_final.py | A (`record_translation_attempt` đổi tên) |
| 65 | **`test_failed_translation_never_reaches_tts`** (guard) | test_v1_tts_translation_guard.py | ? |
| 66 | `test_repairs_only_chinese_before_tts` | test_v1_tts_translation_guard.py | ? |
| 67 | `test_immutable_snapshot_in_frozen_config` | test_v1_video_router.py | B (WIP router) |
| 68 | `test_cache_failure_does_not_fail_audio` | test_v1_voice_ocr_optimization.py | A |
| 69 | `test_invalid_duration_is_never_cached` | test_v1_voice_ocr_optimization.py | A (`_atomic_meta` không tồn tại) |
| 70 | `test_shared_cache_and_corruption` | test_v1_voice_ocr_optimization.py | A |
| 71 | `test_orchestrator_planning_and_gpu_guard` | test_v1_wireup_integration.py | B (SHORT vs LONG) |
| 72 | `test_runtime_times_and_enhancement_failure_fallback` | test_v2_refactor_contracts.py | ? |
| 73 | `test_guard_last_cue_and_speed` (**SUBFAILED**) | test_v1_speech_translation_repair.py | A |

**Đọc bảng:** ~nửa danh sách là test cũ cần cập nhật theo refactor (nhóm A — sửa nhanh, không phải bug hành vi); 6 test nhóm C đã chứng minh pass khi chạy riêng (lỗi hạ tầng test, không phải lỗi sản phẩm); nhóm B gắn trực tiếp khối WIP chưa commit; nhóm D cần môi trường/secret giả. Các test **in đậm** là an toàn then chốt — cần ưu tiên xác minh hành vi thật + cập nhật test cho đúng.

---

## 7. ĐIỂM MẠNH NỔI BẬT (NÊN GIỮ VỮNG)

1. **Kiến trúc pipeline chuyên nghiệp** — manifest/checkpoint/hash, smart resume, timeout theo stage, GPU gatekeeper + VRAM guard, cô lập tiến trình ASR/separator. Vượt trội so với mặt bằng "tool cá nhân".
2. **Đang vận hành production thật và ổn định** — job end-to-end thành công trong ngày đánh giá; tự phục hồi; có QC + delivery guard + cleanup.
3. **Phản ứng sửa lỗi nhanh và đúng trọng tâm** — phần lớn lỗi CRITICAL/HIGH của review 15/10 được xử lý trong ~2 ngày, có test đi kèm (Gemini multikey pool, OCR geometry repair, speech translation repair...).
4. **Tài liệu mới trung thực** — `RC3_REVIEWED_STATUS.md` và `V1_FINAL_VERIFICATION.md` dám nói rõ giới hạn ("chưa benchmark/soak", "không claim speedup") — văn hóa kỹ thuật tốt.
5. **Hệ sinh thái tính năng giàu** — 26 giọng TTS (CapCut/VieNeu/Edge), auto-voice F0, dual voice, script studio, workflow studio, preset/recovery.
6. **Vệ sinh git tốt cho secrets** — `.env` chưa từng bị commit; packing script chủ động loại + kiểm tra chéo secret.

---

## 8. VẤN ĐỀ & RỦI RO THEO MỨC ƯU TIÊN

### 🔴 P0 — Xử lý ngay (độc lập với mọi refactor)

| # | Việc cần làm | Vị trí/Bằng chứng |
|---|---|---|
| P0-1 | **Rotate toàn bộ API key** (Gemini + các key khác) vì 9 dòng key cũ nằm trong `app.log`/`backend/app.log`; sau đó xóa 2 file log cũ (60 MB) và log rotate tự động | `app.log`, `backend/app.log` |
| P0-2 | **Xóa hoặc cô lập `Tool_V1.0_Final_RC2.zip`** (chứa `backend/.env`) — tuyệt đối không chia sẻ file này | thư mục gốc |
| P0-3 | **Commit/stash khối WIP SenseVoice** (16 file) để tách biệt thay đổi với baseline; chạy lại test sau khi commit | `git status` |
| P0-4 | Kiểm tra `server_crash.log` + dòng `asr_worker_total success=False` — đảm bảo không phải lỗi lặp | `backend/server_crash.log`, log 23:04 |

### 🟠 P1 — Quyết định thiết kế fail-closed (then chốt để đóng v1.0.0)

| # | Việc cần làm | Vị trí |
|---|---|---|
| P1-1 | Quyết định chính sách: (a) giữ non-strict nhưng **nâng QC mặc định lên WARN/BLOCK + chặn delivery khi `no_cjk_residual` hoặc `ocr_geometry` FAIL**; hoặc (b) bật strict cho nhóm lỗi nghiêm trọng; sau đó ghi rõ vào README | `v1_orchestrator.py:607, :671`, `v1_feature_flags.py`, `v1_quality_gate.py` |
| P1-2 | Xử lý `no_caption_evidence` trong `validate_mask_geometry`: khi video được phân loại "có phụ đề" (band hỗ trợ một phần) thì trạng thái này phải bị coi là invalid (fail-closed) | `v1_ocr_geometry.py:155-160` |
| P1-3 | Sửa/xác minh các test an toàn then chốt đang đỏ: `test_ocr_fail_closed`, `test_stage6_acceptance_matrix::test_release_gate_zero_exposed_chinese_text`, `test_v1_tts_translation_guard`, `test_rc3_safety`, `test_v1_audio_refactor` | tests/ |
| P1-4 | Bỏ miễn trừ token cho `/api/script/*` (hoặc thêm origin-check như dashboard internal); gỡ/bảo vệ `GET /api/auth-token`; thêm middleware Host+token cho `dashboard_monitor.py` hoặc xóa hẳn file legacy | `main.py:129, :980`, `dashboard_monitor.py` |

### 🟡 P2 — Chất lượng test, vận hành, repo

| # | Việc cần làm | Vị trí |
|---|---|---|
| P2-1 | "Xanh hóa" ~40 test drift (nhóm A): đổi tên thuộc tính/style/format log theo code mới — phần lớn là sửa nhanh 1 dòng/test | tests/ |
| P2-2 | Sửa ô nhiễm test (nhóm C): chuyển `logging.FileHandler` của `telegram_bot.py` vào `if __name__ == "__main__"`, dùng `tmp_path` thay Temp chung | `telegram_bot.py:78-86`, `conftest.py` |
| P2-3 | Mở rộng CI: thêm tối thiểu các module test an toàn (`test_ocr_fail_closed`, `test_v1_quality_gate`, `test_rc3_safety`, `test_v1_tts_translation_guard`, `test_speech_pauses_and_punctuation`) | `.github/workflows/python-quality.yml` |
| P2-4 | Kết thúc WIP SenseVoice (test hybrid, mutual exclusion) trước khi set default `whisper_sensevoice` | `v1_feature_flags.py`, `ai/v1_sensevoice_adapter.py` |
| P2-5 | Dọn rác thư mục gốc: 3 zip release, media test 70MB+, `scratch/` (35 file), `scripts_and_tests/` (61 file), `.git.worktree_bak`, `out.srt` | thư mục gốc, `backend/` |
| P2-6 | Đồng bộ `requirements.txt` (paddleocr 2.x vs 3.7.0; bổ sung dep thiếu), cập nhật `PROJECT_STATE.md` theo trạng thái V1-only | requirements*.txt, docs/ |
| P2-7 | Sửa mojibake log tiếng Việt (một số dòng lỗi mã hóa) để tra log hiệu quả | `backend/app.log` + logger config |
| P2-8 | Bổ sung soak-test 10 video + crash-recovery test có bằng chứng lưu repo (điều kiện tiên quyết đóng tag `v1.0.0`) | tests/ + docs/ |

---

## 9. LỘ TRÌNH ĐỀ XUẤT ĐỂ ĐÓNG TAG `v1.0.0`

1. **Tuần này (P0):** rotate key, xóa log/zip nhạy cảm, commit WIP (hoặc stash), xác minh ASR worker ổn định.
2. **Sau đó (P1):** quyết định chính sách fail-closed + nối QC vào delivery; sửa 5 nhóm test an toàn then chốt; bịt nốt bề mặt token.
3. **Hoàn thiện (P2):** dập 40 test drift + 6 test ô nhiễm (đưa suite về ≥ 95% xanh), mở rộng CI, dọn repo, đồng bộ requirements/docs.
4. **Nghiệm thu cuối:** soak-test 10 video cùng settings (ghi thời gian/RAM/VRAM từng stage như RC3 hướng dẫn), chạy full suite 2 lần liên tiếp xanh ≥ 95% không flake, đóng tag `v1.0.0` kèm manifest + log bằng chứng.

Ước tính: hoàn thành P0 + P1 → điểm lên **~8/10**, đủ điều kiện đóng v1.0.0 với bằng chứng vững.

---

## 10. PHỤ LỤC

### A. Lệnh & bằng chứng chính

```powershell
# 1) Thu thập test
py -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors -x --co   # 887 collected

# 2) Full suite (lần sạch, ghi file)
py -m pytest tests -q --tb=no -p no:cacheprovider --continue-on-collection-errors
# -> 73 failed, 811 passed, 4 skipped, 158 subtests passed in 231.72s

# 3) Xác minh nhóm nghi ô nhiễm (chạy riêng)
py -m pytest tests/test_v1_ocr_proxy.py -x -q      # 3 passed in 0.08s  (full suite: 3 failed)
py -m pytest tests/test_sensevoice_hybrid.py -q    # 3 passed in 6.66s  (full suite: 1 failed)

# 4) Xác minh nhóm lỗi thật (chạy riêng vẫn fail)
py -m pytest tests/test_v1_resource_control.py::ResourceTests::test_stage_context_and_failure -q
# -> 'video_id=abc123' not found in log format mới

# 5) Compile sanity
py -m compileall -q backend tests                   # exit 0

# 6) Render history format: test drift
# test_v1_translation_final.py: AttributeError: module 'job_tracker' has no attribute 'record_translation_attempt'

# 7) README claim (kiểm chứng)
py -m pytest tests/test_verify_production_readiness.py tests/test_v1_quality_gate.py -q
# -> 12 passed in 5.33s  ✅ khớp README
```

### B. Danh mục bằng chứng file:line quan trọng

| Nội dung | Vị trí |
|---|---|
| Orchestrator tắt strict OCR | `backend/v1_orchestrator.py:607` |
| Orchestrator tắt strict dịch | `backend/v1_orchestrator.py:671` |
| Delivery guard chỉ verify media | `backend/v1_orchestrator.py:953-958` |
| `no_caption_evidence` không bao giờ invalid | `backend/v1_ocr_geometry.py:155-160` |
| Auto-heal gán median + dán nhãn located | `backend/v1_ocr_geometry.py:114-154` |
| OCR không thấy dải phụ đề → best_block=None | `backend/ocr_utils.py:695-704` |
| TTS raise sau retry (không silent fallback) | `backend/ai/voice_cloning.py:112-135` |
| Cache chặn audio câm | `backend/ai/v1_voice_cache.py:46-48` |
| Dịch: default fallback = false | `backend/ai/translation.py:945, :1004` |
| Mask key trong log | `backend/ai/translation.py:415` |
| CJK Extension A | `backend/v1_quality_gate.py:93` |
| CORS lọc "null" | `backend/main.py:58` |
| Token compare_digest + ngoại lệ còn lại | `backend/main.py:129-139` |
| `GET /api/auth-token` không auth | `backend/main.py:980-983` |
| Containment check script_api | `backend/script_api.py:99-118, :852-868` |
| dashboard_monitor :8089 không auth | `backend/dashboard_monitor.py:121, :1686` |
| FileHandler log production khi import | `backend/telegram_bot.py:78-86` |
| CI chỉ chạy 3 module | `.github/workflows/python-quality.yml` |
| GPU gatekeeper | `backend/v1_gpu_gatekeeper.py:63-80` |
| Manifest/smart resume | `backend/v1_orchestrator.py:401-417` |

### C. Ghi chú phương pháp (công bằng & minh bạch)

- Đánh giá thực hiện trên **cây làm việc WIP** (16 file chưa commit). Một phần kết quả (nhóm B) sẽ đổi sau khi commit/hoàn thiện SenseVoice.
- 2 lần chạy full suite cho 72 vs 73 failed — bộ test có flakiness; con số "73" lấy từ lần chạy sạch ghi file đầy đủ.
- Việc phân loại nhóm là **ước lượng có cơ sở** (dựa trên mẫu đã xác minh trực tiếp); các ô "?" cần chạy riêng để phân loại dứt điểm.
- Báo cáo phản ánh đúng trạng thái: các fix mới đã được ghi nhận tích cực (TTS/cache/translation/CJK/security), không đánh giá theo cảm tính từ báo cáo cũ.

---

*Hết báo cáo — sinh ngày 2026-10-17 bởi phiên review tự động.*
