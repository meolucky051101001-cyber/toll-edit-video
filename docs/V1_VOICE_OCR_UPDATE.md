# V1 voice / OCR follow-up — 2026-09-12

Applied to source, not the old RC3 archive. No models, provider order, prompts,
render settings, V2 or existing media changed. Existing cache is not deleted.

## Voice

- Rolling bounded cue scheduler refills free slots immediately, preserving input
  order and per-cue errors. RAM limits and provider/RVC semaphores still apply.
- Cancellation drains admitted coroutine tasks; no unbounded cue-task creation.
- Cache version 5 includes exact target duration, voice/source, model/index file
  metadata and a voice implementation fingerprint. Old cache entries become misses.
- Validate size, finite duration and SHA256 before reuse. Publish audio/metadata
  via unique temporary files and atomic replacement. A race/mismatch is a miss.
- Cache errors do not fail already produced audio. No subtitle content in new
  cache metadata or cache-hit logs. Reused audio bypasses both TTS and RVC.
- FPT/CapCut fallback and outer emergency Edge output are not cached under the
  originally requested voice. Provider choices themselves remain unchanged.
- Keep the existing one RVC instance per voice worker and release it at the end;
  do not keep a model across videos while RAM pressure remains unmeasured.

## OCR

- Hash contiguous frame buffers without creating a second full byte string.
- Include dtype and shape in exact-pixel cache keys. A single changed pixel is
  not merged. Recognition cache still keeps at most 64 entries, reset per video.
- Raw captured-frame budget: 8 MiB under 2 GiB available (or unknown), 24 MiB under
  4 GiB, otherwise 48 MiB. Batch count remains 1–12; one large frame may exceed
  the raw budget. This does not bound model allocations.
- Copy unscaled crops so queued images don't retain larger decoded frame arrays.
- Preserve the current timestamps, sample count, 5–95% crop, resize and proxy
  policy. These existing settings are not a guarantee of catching every subtitle.

## Validation

61 Python tests passed, plus dashboard JS checks and syntax compilation.
New tests cover rolling refill, exact-duration keys, corrupt shared cache, cache
I/O failure, invalid durations, OCR budgets and changed-pixel recognition.
No live API/model calls, media benchmark or perceptual quality assessment performed.

## Still gated, not implemented in this change

Cross-video warm RVC, cue concatenation, adaptive sample-rate reduction, learned
subtitle ROI and approximate-frame skipping require reference-video tests first.
Next compare identical cold/warm video runs, cue coverage, audio alignment and
sampled memory. First run can be slower because old unsafe cache is invalidated.
