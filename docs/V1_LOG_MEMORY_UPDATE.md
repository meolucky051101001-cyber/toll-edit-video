# V1: log and memory control update

Implemented 2026-09-12 in the live source tree. The existing RC3 zip is unchanged;
it does not include this follow-up. Models, prompts, provider order, credentials,
V2, originals and finished videos were not changed.

## Measurements

- A fresh video_id per tracker video. Stage metrics propagate it into batch-owned
  subprocesses using an explicit environment copy (no global environment mutation).
- A single 1-second sampler per tracked video observes the current process and its
  descendants. Dashboard is excluded for Telegram work; direct API work includes
  its server process and is labelled as a process-tree measurement.
- resource_summary and step_durations are stored in existing job status/history.
  Sampled peak RSS is a sum: shared pages may be counted twice; this is NOT unique
  RAM, VRAM or an exact peak. Available RAM is a system-wide observation, not V1 use.
- Wall time is measured independently, never by summing nested/concurrent stages.
- Separate RVC queue wait, explicit model loading and inference log events.
  Lazy model initialization inside infer_file remains part of inference time.
- Repeated updates to the same step no longer reset its clock. Historical logs
  cannot overwrite the currently displayed step clocks.
- Voice-worker logs use timestamped records, and the UI also reads legacy worker
  records. Gemini attempt/error messages are valid Vietnamese and have video IDs.
  Existing corrupted historical log lines and prompt strings are not rewritten.

## Memory admission

- Cue tasks use rolling bounded admission, not one task for every subtitle.
- Available RAM <2 GiB (or unavailable measurement): 1 cue; <4 GiB: 2; otherwise 4.
  Existing provider/RVC semaphores still apply. In-flight tasks are not interrupted
  when RAM drops; new admission adapts. A finished cue frees its slot immediately.
- Before ASR/OCR/voice work, available RAM below 768 MiB waits up to 20 seconds,
  checking cooperative cancellation. If still low, fail with a clear message,
  without deleting videos or killing other programs. These are conservative initial
  thresholds, not guarantees against out-of-memory conditions.
- Existing isolated model workers and cleanup are retained. No automatic increase
  in video concurrency and no model caching policy change.
- Samplers stop on completion, error and normal stop; test coverage checks lifecycle.

## Validation and next gate

Isolated resource tests cover bounded order/concurrency/errors/cancellation, RAM
thresholds, sampler lifetime, same-step timing, metric IDs and worker propagation.
RC3 and dashboard regression tests are run separately.
Combined Python verification: 40 passed (3 existing dependency deprecation warnings).
The first run from the desktop workspace hit an unrelated relative temp_dir path
failure in a downloader test; rerunning from an isolated temporary directory passed.
No real translation, TTS or GPU workload is submitted by these tests.

Next: run 10 user-supplied videos sequentially, inspect memory after every video,
compare identical cold/warm inputs, and listen for missing/shifted speech.
Do not claim reduced peak RAM or faster processing until measured live.
