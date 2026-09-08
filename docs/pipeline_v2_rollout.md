# Pipeline v2 rollout guide

Pipeline v2 is integrated into Telegram URL uploads, Telegram file uploads and
the local batch processor. The default remains the unchanged legacy pipeline.

## Modes

```env
PIPELINE_MODE=legacy
```

- `legacy`: current production flow; no v2 processing or manifest writes.
- `shadow`: legacy remains authoritative and a post-run shadow manifest records
  artifact hashes plus observed stage/total timing for comparison. It never
  loads a second GPU model alongside the live legacy render.
- `v2`: use the checkpointed v2 pipeline and publish its final result.

Restart the bot only when its queue is empty. Never change modes while Demucs,
Whisper, EasyOCR, RVC or FFmpeg is active.

## Preflight bắt buộc

Tạo `backend/.env` từ `backend/.env.example`, điền secret thật và đặt workspace
ngoài OneDrive/thư mục đồng bộ đám mây. Sau đó chạy bằng đúng Python trong venv:

```powershell
cd backend
.\venv\Scripts\python.exe -m pipeline_v2.preflight --project-root .. --interface all
```

Chỉ bắt đầu video thật khi dòng cuối báo `ready=True`, `pipeline:config` là `v2`
và không còn mục `error`. Warning về RVC có thể chấp nhận nếu chỉ dùng Edge/FPT;
warning CUDA nghĩa là pipeline có thể chạy CPU nhưng không phù hợp video dài.
Wildcard CORS bị preflight chặn vì API nhận đường dẫn file cục bộ.

## Recommended rollout

Start with shadow mode:

```env
PIPELINE_MODE=shadow
```

Then test one short video with conservative v2 settings:

```env
PIPELINE_MODE=v2
ENABLE_GPU_PROCESS_ISOLATION=true
ENABLE_STAGE_CACHE=true
ENABLE_PARALLEL_OCR_GEMINI=false
ENABLE_ADAPTIVE_DEMUCS=false
ENABLE_ADAPTIVE_OCR=false
ENABLE_TIMING_SOLVER=false
ENABLE_FFMPEG_MIX_V2=false
ENABLE_LEGACY_MIX_AB=false
PRESERVE_SOURCE_RESOLUTION=true
QC_GATE_POLICY=report_only
ATEMPO_MIN=0.92
ATEMPO_MAX=1.08
```

Enable optimizations one at a time after comparing the output:

```env
ENABLE_PARALLEL_OCR_GEMINI=true
ENABLE_ADAPTIVE_DEMUCS=true
ENABLE_ADAPTIVE_OCR=true
ENABLE_TIMING_SOLVER=true
ENABLE_FFMPEG_MIX_V2=true
```

API desktop xử lý tuần tự để hai request không tranh VRAM/FFmpeg hoặc ghi đè
workspace cùng lúc. Electron dùng preload bridge cô lập; React không có quyền
Node trực tiếp. Các origin API mặc định chỉ gồm UI localhost và origin `null`
của bản Electron đóng gói; không cấu hình `AUTODUB_CORS_ORIGINS=*`.

When FFmpeg mix v2 is enabled, Pydub A/B output stays off by default to avoid
loading long PCM audio into RAM twice. Set `ENABLE_LEGACY_MIX_AB=true` only for
a deliberate listening comparison.

Adaptive decisions are conservative. An uncertain audio probe runs Demucs; an
unavailable lightweight video probe runs EasyOCR. Source resolution is kept,
and a requested target must never upscale a smaller source.

Keep QC non-blocking during initial real-video testing:

```env
QC_GATE_POLICY=report_only
```

After multiple successful videos, `warn` still delivers while surfacing QC
errors. `block` prevents delivery when the QC report contains an error, but it
does not delete the render or its artifacts:

```env
QC_GATE_POLICY=block
```

## Checkpoints and output

Each legacy job directory gains a separate `pipeline_v2` directory containing:

- `job_manifest.json` with stage status, attempts and errors;
- `artifacts/` with immutable-by-hash stage outputs;
- `control/` for short-lived GPU worker requests;
- `work/` for stage-local temporary directories.

An interrupted `running` stage becomes `failed` on restart and is retried from
that stage. In `PIPELINE_MODE=v2`, Telegram startup scans incomplete manifests,
puts them back into the same sequential work queue, and republishes both the
workspace output and the automatic `D:\banve` copy. Completed artifacts are
reused only when SHA-256, source,
configuration and model fingerprints remain valid. `ENABLE_STAGE_CACHE=true`
also reuses a fully delivered job; when false, a new run archives the completed
manifest and starts fresh.

The GPU lock is shared by Demucs, Whisper, EasyOCR and RVC. Each heavyweight
stage runs in its own Python process, and operating-system lock release protects
against worker crashes.

Pipeline v2 dùng fail-closed cho các artifact tạo nội dung: Demucs/RVC giả hoặc
Git LFS pointer, bản dịch CJK không đổi, batch OCR/Translation rỗng và FPT hết
quota đều làm stage thất bại thay vì âm thầm xuất media sai. Resume giữ nguyên
nguồn giọng đã ghi trong manifest; job RVC thiếu model sẽ dừng để người vận hành
khôi phục đúng model. Callback Telegram/UI lỗi chỉ được ghi warning (giữ tối đa
50 bản gần nhất) và không làm hỏng media job.

## Duration-independent resource limits

Pipeline v2 does not use a separate long-video mode or change the render steps.
The same pipeline scales its timeout from ffprobe duration and bounds the work
per batch, so a 30–60 minute input does not create an unbounded API request,
task list, model process or FFmpeg command line:

```env
TRANSLATION_BATCH_SEGMENTS=80
TRANSLATION_BATCH_CHARACTERS=12000
OCR_BATCH_SEGMENTS=80
TTS_BATCH_SEGMENTS=64
RVC_BATCH_SEGMENTS=64
MIXER_CHUNK_SECONDS=300
MIXER_MAX_INPUTS_PER_PASS=64
```

- Every Gemini translation batch keeps three prior translated lines and samples
  five frames from that batch's own time range.
- TTS and RVC publish atomic per-batch checkpoints. Restarting after a later
  batch fails does not regenerate already validated audio batches.
- RVC uses one short-lived isolated model process per bounded batch.
- The studio mixer builds a lossless FLAC voice bus in bounded FFmpeg passes,
  then applies the same sidechain ducking, `-15 LUFS` loudness and `-1 dBTP`
  limiter once to the full program.
- The global yt-dlp wall-clock timeout is disabled by default; socket timeout
  and retry rules remain active. Set `SOCIAL_DOWNLOAD_TIMEOUT_SECONDS` only if
  an explicit total download deadline is required.

These limits control peak resources, not video duration. They may be tuned for
the machine without changing stage order or output behavior.

## Checklist trước khi chuyển sang hoạt động

1. Preflight `ready=True` bằng chính venv sẽ chạy bot/API.
2. Chạy một video 30–90 giây ở `QC_GATE_POLICY=report_only`; mở và nghe file cuối,
   kiểm tra `qc/qc_report.json` và `job_manifest.json`.
3. Dừng giữa TTS hoặc RVC rồi khởi động lại để xác nhận resume không đổi giọng và
   không chạy lại các stage đã có SHA-256 hợp lệ.
4. Chạy lần lượt Edge, FPT (nếu dùng) và RVC (nếu dùng); không coi một provider
   đã qua là bằng chứng cho provider khác.
5. Chạy một video dài đại diện với cấu hình batch thật và theo dõi VRAM, dung
   lượng workspace, thời gian mixer/render.
6. Sau khi ma trận video thật sạch, chuyển `QC_GATE_POLICY=warn`, rồi `block`.

Không đánh dấu production-ready chỉ từ unit test: Demucs, dịch/TTS cloud, RVC và
NVENC cần ít nhất một lượt end-to-end với model, API key và driver của máy chạy.

## Rollback

Set the following and restart after the queue becomes empty:

```env
PIPELINE_MODE=legacy
```

Legacy mode imports only the lightweight mode configuration; no v2 model,
worker or media-processing module is loaded. The legacy Telegram and batch
processing bodies are still present. Existing v2 artifacts can be kept for
diagnosis and resume; they do not alter legacy outputs.
