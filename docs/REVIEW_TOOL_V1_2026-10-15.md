# BÁO CÁO REVIEW & ĐÁNH GIÁ TOOL V1

- **Đối tượng review:** `C:\tool v1` (nhánh `tool-v1`, HEAD `61bc7b0`)
- **Ngày review:** 2026-10-15
- **Người/Nguồn thực hiện:** AI Code Review (OpenCode)
- **Phạm vi:** Kiến trúc, correctness fail-closed, bảo mật API local, chất lượng test, đóng gói/phát hành, vệ sinh repo
- **Trạng thái cây làm việc khi review:** Bẩn (19 file sửa dở + 2 file mới chưa track)

> Mọi kết luận trong báo cáo đều kèm bằng chứng `file:dòng`. Các mục có ghi rõ khi là suy luận/một phần.

---

## 1. Tóm tắt điều hành (Executive Summary)

Tool V1 có **kiến trúc tốt** và nhiều subsystem được làm nghiêm túc (ASR cô lập tiến trình, Gemini dispatcher có circuit breaker, downloader atomic, checkpoint/resume). Tuy nhiên, tại thời điểm review, dự án **chưa đạt trạng thái "V1.0 Final"** như tài liệu trong repo tuyên bố:

1. **3/4 đảm bảo fail-closed cốt lõi (OCR / TTS / Translation) vẫn hở hoặc vừa bị hạ cấp** trong cây làm việc chưa commit.
2. **Bề mặt API local có nhiều lỗ hổng nghiêm trọng** (đọc file tùy ý không cần auth, CSRF qua CORS `null`, dashboard :8089 không xác thực, lộ API key trong log).
3. **Bộ test đang đỏ nặng**: `110 failed / 750 passed / 3 skipped / 4 collection errors`, trái ngược hoàn toàn với tuyên bố "198/198 PASS".
4. **CI không chạy trên nhánh `tool-v1`** và không chạy full suite.
5. **Tài liệu mô tả frontend React/Vite/Electron không tồn tại** (`frontend/` chỉ có `README.md`).

**Khuyến nghị:** KHÔNG đóng tag `v1.0.0` ở trạng thái hiện tại. Cần xử lý nhóm P0 (bảo mật) và P1 (fail-closed) trước.

---

## 2. Phạm vi & phương pháp

| Hạng mục | Thực tế |
|---|---|
| Nhánh | `tool-v1` (HEAD `61bc7b0`) |
| Quy mô mã nguồn | 452 file `.py` (không tính backup/venv), ~86.000 dòng |
| Điểm vào chính | `backend/main.py` (:8088), `backend/dashboard_monitor.py` (:8089), `backend/telegram_bot.py`, `backend/tool_control.py` (:8090), `backend/batch_processor.py`, `KhoiDongAn.vbs` |
| Cách kiểm chứng | Đọc source, `python -m compileall backend tests` (PASS), chạy full pytest bằng venv của dự án |

### Kết quả chạy test thực tế

Lệnh:

```powershell
cd "C:\tool v1"
.\backend\venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors
```

Kết quả:

```text
110 failed, 750 passed, 3 skipped, 4 errors, 152 subtests passed in 292.52s
```

### 4 module không collect được (ImportError)

| Test | Thiếu hàm trong source |
|---|---|
| `tests/test_code_review_fixes.py` | `_subdivide_and_retry` |
| `tests/test_translation_resilience.py` | `_parse_json_array` |
| `tests/test_model_upgrades.py` | `_aligned_units_to_segments` |
| `tests/test_telegram_bot_v2_delivery.py` | `process_v2_telegram_job` |

---

## 3. Điểm mạnh (đã xác minh trong code)

- **ASR cô lập tiến trình đúng:** `backend/ai/v1_asr_isolated.py` spawn subprocess worker, có retry khi `STATUS_ACCESS_VIOLATION`, giải phóng VRAM sau stage. `backend/ai/transcription.py:318-326` ép `num_workers=1` trên CUDA và **cấm fallback CPU** (fail-closed).
- **Gemini dispatcher** (`backend/ai/v1_gemini_dispatcher.py`): circuit breaker, cooldown 429 tôn trọng `Retry-After`, đa key failover, deadline đơn điệu, mask secret trong log.
- **Không có command injection:** toàn bộ `subprocess` dùng list-argv, không `shell=True`; `os.system` chỉ nối PID dạng số.
- **Không có deserialization nguy hiểm:** không `pickle.load`, không `torch.load(weights_only=False)`, không `eval` dữ liệu người dùng.
- **Token controller :8090** (`backend/tool_control.py`) đúng chuẩn: `secrets.token_urlsafe`, `compare_digest`, kiểm tra Host, giới hạn body.
- **Downloader:** atomic temp → validate size → ffprobe → `os.replace`, có timeout, sanitize URL/exception (`backend/social_downloader.py:84-125`).
- **Không có secret bị commit:** `.env` / `backend/.env` nằm trong `.gitignore`, chưa từng được track.

---

## 4. Phát hiện theo mức độ nghiêm trọng

### 🔴 CRITICAL — Bảo mật API local

#### C1. Path traversal + đọc file tùy ý, không cần auth
**Vị trí:** `backend/script_api.py:818-849`

```python
@router.get("/api/script/stream/{file_path:path}")
def api_stream_script_file(file_path: str):
    candidates = [SCRIPT_WORKSPACE / file_path, ...]   # không containment check
    ...
    target = cand.resolve()
```

`file_path` được nối thẳng, không có `resolve().is_relative_to(root)`. Middleware token **miễn trừ toàn bộ `/api/script/`** (`backend/main.py:128`), và đây là `GET`. Hệ quả: đọc được `.env`, `.dashboard_control_token` → leo thang lấy token điều khiển.

#### C2. CORS cho phép `Origin: null` + `GET /api/auth-token` không auth
**Vị trí:** `backend/main.py:52-67`, `backend/main.py:966-969`

```python
default CORS: ... "http://localhost:8088,null"
```

```python
@app.get("/api/auth-token")
async def api_get_auth_token():
    return {"token": LOCAL_TOKEN}
```

Bất kỳ trang web nào cũng có thể mở iframe `sandbox` (origin `null`), gọi `http://127.0.0.1:8088/api/auth-token`, đọc token (do `Access-Control-Allow-Origin: null`), rồi điều khiển mọi endpoint ghi → leo thang lên điều khiển toàn bộ tool.

#### C3. Middleware token chủ động miễn trừ chính các endpoint ghi
**Vị trí:** `backend/main.py:127-131`

```python
if not path.startswith("/api/script/") and path != "/api/voice-preview" \
   and not path.startswith("/api/jobs/") \
   and path not in ("/api/stop-batch","/api/pause-video","/api/resume-video",
                    "/api/audio-settings","/api/worker-settings"):
```

Miễn trừ nhiều endpoint ghi, trong đó:
- `/api/stop-batch` không có cả kiểm tra Origin → CSRF bằng form đơn giản.
- `/api/jobs/*`: `POST /api/jobs/batch` kích hoạt batch, `POST /api/jobs/{id}/resume` khởi chạy xử lý video.
- `/api/audio-settings`, `/api/worker-settings`.

Tài liệu Phase 5 tuyên bố "bắt buộc token cho toàn bộ state-changing requests" — **không đúng**.

#### C4. Dashboard :8089 hoàn toàn không có xác thực
**Vị trí:** `backend/dashboard_monitor.py:121`

```python
app = FastAPI()   # không middleware, không CORS, không token
```

Bind `127.0.0.1:8089` (`uvicorn.run(app, host="127.0.0.1", port=8089)`). Mọi endpoint `/api/run-batch`, `/api/stop-batch`, `/api/retry-video`, `/api/open-folder`, `/api/queue/*` đều mở. Ngoài ra `/api/retry-video` (`:950-954`) nối `filename` trực tiếp vào path mà **không kiểm tra basename** → path traversal.

#### C5. API key Gemini bị ghi ra log và log không cần auth
**Vị trí:** `backend/ai/translation.py:341` + `backend/main.py:356`

```python
logger.warning(f"Lỗi dịch Gemini: {e}")   # exception chứa ...generateContent?key=<GEMINI_API_KEY>
```

Xác nhận trong `app.log`: **9 dòng** chứa `generateContent?key=...`. `GET /api/logs` trả 500 dòng cuối **không cần token**. Repo đã có `mask_secret` / `sanitize_text` nhưng không áp dụng ở đây.

#### C6. `_validate_safe_video_path` không giới hạn thư mục cho phép
**Vị trí:** `backend/script_api.py:87-97`

Chỉ kiểm tra tồn tại + phần mở rộng, chấp nhận **mọi đường dẫn video tuyệt đối** trên máy. `/api/script/analyze-video` (miễn token) còn upload file lên Gemini → vector exfiltration dữ liệu cục bộ.

---

### 🟠 HIGH — Correctness / tính toàn vẹn pipeline

#### H1. OCR vẫn fail-open
**Vị trí:** `backend/ocr_utils.py:686-695`

```python
else:
    logger.info("No reliable Chinese subtitle band detected ...")
    main_y_pct = 0.85
    if srt_segments:
        for seg in srt_segments:
            seg.best_block = None
            seg.tracking_blocks = []
            seg.y_pct = 0.85
            seg.max_y_pct = 0.90
            seg.ocr_mask_status = 'no_caption_evidence'
```

`best_block = None` ⇒ ASS không vẽ hộp che ⇒ chữ Trung gốc có thể lộ. Đúng lỗi Critical #1 mà master plan yêu cầu loại bỏ.

#### H2. `validate_mask_geometry` tự "chữa" rồi dán nhãn lại `located`
**Vị trí:** `backend/v1_ocr_geometry.py:110-168`

Khi ≥50% cue "located", mọi cue unresolved bị gán tọa độ median và đổi thành `'located'` (`:145, :154`). Hàm `invalid` (`:155-160`) chỉ xét `'unresolved'`/`'located'` — **`'no_caption_evidence'` không bao giờ bị coi là invalid** → `V1_STRICT_OCR_GEOMETRY` (mặc định bật) gần như không bao giờ raise. `OCRGeometryError` trở thành code chết trong tình huống phổ biến.

#### H3. TTS vẫn tạo "silent audio" giả thành công
**Vị trí:** `backend/ai/voice_cloning.py:120-129` (và caller `:534/536/541`)

```python
if "NoAudioReceived" in type(exc).__name__ or "empty audio" in str(exc).lower():
    ... ffmpeg anullsrc ...
    if res.returncode == 0 and os.path.getsize(output_path) > 0:
        return True
```

Caller không kiểm tra giá trị trả về → cue lỗi được coi là thành công. `v1_voice_cache.is_valid_audio` (`:35-52`) chỉ kiểm tra "decode được + duration ≥ 0.15s" nên **audio im lặng được ghi cache** (`voice_cloning.py:724-726`) và tái sử dụng. Đúng Critical #2 của master plan.

#### H4. Fail-closed dịch thuật vừa bị làm yếu trong cây làm việc chưa commit
**Vị trí:** `backend/ai/translation.py`

```diff
- raise RuntimeError("Strict translation mode requires a complete LLM translation ...")
+ logger.warning("... Proceeding with sanitized translation to avoid job failure.")
...
- raise RuntimeError("Strict translation mode requires a complete LLM translation without machine fallback")
+ allow_fallback = os.getenv("V1_ALLOW_TRANSLATION_FALLBACK", "true") ...
```

Cộng với `v1_orchestrator.py:646` gọi `translate_subtitles(..., strict=False)`, khi mọi provider lỗi thì **phụ đề tiếng Trung gốc vẫn được ghi ra** `translated.srt`/`translation.json` (chỉ raise khi `strict=True`). Trái nguyên tắc "không hy sinh correctness để video chạy cho xong".

#### H5. Hai hàm được gọi nhưng KHÔNG được định nghĩa → nhánh OpenAI/DeepSeek chết
**Vị trí:** `backend/ai/translation.py:393,404,459,470`

`_parse_json_array` và `_subdivide_and_retry` được gọi nhưng **không tồn tại ở bất kỳ file nào trong repo** (đã grep toàn bộ). Nằm trong `try/except Exception` nên `NameError` bị nuốt và chỉ log warning ⇒ hai provider fallback luôn thất bại âm thầm.

#### H6. Phát hiện CJK quá hẹp
**Vị trí:** `backend/ai/translation.py:64-65`, `backend/v1_quality_gate.py:90-94`

Chỉ quét `U+4E00–U+9FFF`. CJK Extension A (`U+3400–U+4DBF`) lọt qua mọi cổng fail-closed (gồm `no_cjk_residual` và guard ở `backend/ai/v1_voice_isolated.py:12-13`).

---

### 🟠 HIGH — Chất lượng bộ test & phát hành

- **Suite đỏ nặng:** `110 failed / 750 passed / 4 errors`.
- **Test an toàn then chốt FAIL:**
  - `test_ocr_fail_closed::test_both_telegram_ocr_handlers_raise`
  - `test_subtitle_layout::test_cleanup_does_not_disguise_an_untranslated_chinese_response`
  - `test_final_acceptance::test_gemini_failures_have_bounded_attempts_and_no_query_key`
  - `test_rc3_safety::test_history_live_lock_cannot_be_stolen`
- **CI không chạy trên nhánh này:** `.github/workflows/python-quality.yml` chỉ trigger `[main, tool-v2]`; khi chạy cũng chỉ ruff 3 file + unittest 5 module, không full suite, không build frontend.
- **~40 file test kiểm tra bằng đọc source/AST** (giòn, vỡ khi refactor dù hành vi không đổi); nhiều test hardcode `C:\tool v1`.
- **Test không hermetic:** log cho thấy test gọi mạng thật tới Gemini bằng key trong `.env`.
- **`requirements.txt` thiếu import trực tiếp:** `soundfile`, `scipy`, `scikit-image`, `onnxruntime`, `google-genai`, `pydantic`, `safetensors`, `transformers`, `gmssl`, `torch`.
- **Xung đột phiên bản:** `requirements.txt` pin `paddleocr>=2.7,<3` nhưng `backend/requirements-models.txt` pin `paddleocr==3.7.0`.
- **`frontend/` rỗng** (chỉ có `README.md`) nhưng README/CI vẫn mô tả React/Vite/Electron + `npm ci/build`.

---

### 🟡 MEDIUM — Vệ sinh repo & bảo trì

- `workspace/` **40.3 GB / ~60k file**; `backups/` 181 file (gồm 22 bản `.bak_*` module); `scratch/` 34 script debug; `backend/scripts_and_tests/` 177 script lịch sử.
- Package **trùng lặp**: `backend/whiteboard_animator/whiteboard_animator/` (21 file, 11 module trùng tên).
- Rác ở gốc repo: `app.log` 22.5 MB, patch 800KB–1MB, zip release, media test (~65MB), `master_plan.txt`.
- Kill process theo substring `c:\tool v1` (`backend/tool_control.py:177-265`, `process_killer.py`) có thể diệt nhầm process không liên quan.
- So sánh token không dùng `compare_digest` ở `backend/main.py:130`.
- `sanitize_job_id` (`backend/segment_editor_service.py:143-150`) cho phép dấu phân cách đường dẫn → ghi/đọc JSON ngoài drafts dir qua `/api/jobs/` (miễn token).

---

## 5. Đối chiếu checklist "Tool V1 Final" (master_plan Phase 7/8)

| Yêu cầu | Trạng thái | Ghi chú |
|---|---|---|
| Không render khi OCR lỗi | ❌ | `ocr_utils.py:686-695`, `v1_ocr_geometry.py:110-168` |
| Không giả TTS silent thành công | ❌ | `voice_cloning.py:120-129` + cache silence |
| Không để phụ đề Trung lọt qua dịch | ❌ | `strict=False` + cây làm việc hạ cấp thành warning |
| Telegram không leak GPU process | ✅ (một phần) | ASR/voice isolated, CUDA worker=1 |
| Batch không corrupt history | ⚠️ | Có lock/atomic nhưng `test_rc3_safety` fail |
| Download không treo vô hạn | ✅ | Timeout + atomic |
| Dashboard không block khi xử lý | ✅ | Dùng `asyncio.to_thread` |
| API local được bảo vệ | ❌ | C1–C6 |
| Source không chứa secret (trong git) | ✅ | `.env` được ignore, chưa từng track |
| Source cài được trên máy sạch | ⚠️ | requirements thiếu dep; frontend rỗng |
| Full regression pass | ❌ | 110 failed / 4 errors |
| Telegram/Batch E2E, GPU soak, crash recovery | ❓ | Không có bằng chứng tái lập trong repo |
| Tag v1.0.0 | ❌ | Chỉ có `v1.0-stable-baseline`, `pre-phase-9.1` |

---

## 6. Đánh giá tổng thể

| Tiêu chí | Điểm | Nhận xét |
|---|---|---|
| Kiến trúc | 7/10 | Tách stage rõ; orchestrator + manifest + checkpoint + model registry tốt |
| Đúng đắn nghiệp vụ (fail-closed) | 3/10 | 3/4 đảm bảo cốt lõi vẫn hở hoặc vừa bị hạ cấp |
| Bảo mật | 2/10 | Nhiều lỗ hổng auth/traversal/lộ key trên bề mặt local API |
| Ổn định / phục hồi | 6/10 | Checkpoint/atomic/lock khá; nhưng test an toàn đang đỏ |
| Chất lượng test | 3/10 | Test giòn/AST-scraping, suite đỏ, không hermetic, CI không phủ nhánh |
| Khả năng cài đặt lại | 4/10 | requirements thiếu dep, frontend rỗng, docs lệch thực tế |
| Vệ sinh repo | 3/10 | Backup/scratch/trùng lặp/40GB runtime lẫn trong repo |
| Tài liệu | 4/10 | Nhiều & chi tiết nhưng tuyên bố sai ("198/198 PASS", "0 lỗi Critical/High") |

**Kết luận:** Tool V1 có kiến trúc tốt nhưng **chưa đạt "V1.0 Final"**. Rủi ro lớn nhất là **khoảng cách giữa lời tuyên bố và thực tế kiểm chứng**.

---

## 7. Khuyến nghị ưu tiên

### P0 — Bảo mật (làm ngay, độc lập với refactor)
1. `script_api.py`: chuẩn hóa `resolve()` + `is_relative_to(SCRIPT_WORKSPACE)` cho mọi endpoint nhận path; allowlist extension.
2. Bỏ `"null"` khỏi CORS; **bắt buộc token cho `GET /api/auth-token`** (hoặc bỏ endpoint, inject token vào HTML).
3. Bỏ miễn trừ token cho `/api/script/*`, `/api/jobs/*`, `/api/voice-preview`, `/api/stop-batch`, `/api/audio-settings`, `/api/worker-settings`; dùng `secrets.compare_digest`.
4. Thêm middleware token + Host check cho `dashboard_monitor.py` (:8089) và kiểm tra basename ở `/api/retry-video`.
5. Redact secret trước khi log (`translation.py:341`), yêu cầu token cho `GET /api/logs`, và **rotate toàn bộ key** đang có trong `app.log`/`.env`.

### P1 — Correctness fail-closed
6. OCR: khi `support==0` hoặc cue thiếu bằng chứng → **raise**; coi `no_caption_evidence` là invalid trong `validate_mask_geometry`.
7. TTS: xóa nhánh sinh `anullsrc`; truyền cờ `is_silent_fallback` và cho job fail; cấm cache audio im lặng.
8. Khôi phục fail-closed dịch thuật: trả lại `raise` khi quality check fail và khi `strict=True`; đặt `V1_ALLOW_TRANSLATION_FALLBACK=false` mặc định.
9. Định nghĩa `_parse_json_array` / `_subdivide_and_retry` (hoặc bỏ nhánh OpenAI/DeepSeek).
10. Mở rộng `_contains_cjk` sang Extension A.

### P2 — Test & phát hành
11. Sửa 4 lỗi collection + 110 test fail; thay test AST-scraping bằng test hành vi; cô lập test khỏi mạng.
12. Bật CI cho nhánh `tool-v1` chạy full suite; bổ sung các dep thiếu vào `requirements.txt`.
13. Quyết định frontend (khôi phục hoặc xoá hẳn) và sửa tài liệu cho khớp; dọn `backups/`, `scratch/`, `scripts_and_tests/`, package trùng lặp, rác gốc repo.
14. Commit hoặc revert các thay đổi chưa commit.

---

## 8. Phụ lục — Lệnh & bằng chứng

```powershell
# Quy mô mã nguồn
Get-ChildItem -Recurse -Filter *.py | ...   # 452 file, ~86.000 dòng

# Static check
.\backend\venv\Scripts\python.exe -m compileall -q backend tests   # exit 0

# Full test suite
.\backend\venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors
# -> 110 failed, 750 passed, 3 skipped, 4 errors

# Kiểm tra hàm không tồn tại
Select-String -Path backend\ai\translation.py -Pattern "_parse_json_array|_subdivide_and_retry"

# Kiểm tra lộ key trong log
Select-String -Path app.log -Pattern "generateContent\?key="   # 9 dòng
```

### Lưu ý về phương pháp (để công bằng)
- Một phần trong 110 test fail là **test giòn/cũ** (kiểm tra chữ trong source) chứ không hẳn lỗi hành vi; số còn lại gồm các test an toàn then chốt thực sự đỏ.
- Một số test phụ thuộc GPU/model/mạng có thể fail do môi trường; tuy nhiên `test_final_acceptance`, `test_subtitle_layout`, `test_rc3_safety` fail vì **bất đồng giữa code và kỳ vọng test**, không phải thiếu tài nguyên.
- Cây làm việc đang bẩn (19 file sửa dở) là nguyên nhân của một phần lớn lỗi — bản thân điều này cũng là rủi ro phát hành.
