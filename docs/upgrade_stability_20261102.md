# Tool V1 stability upgrade — 2026-11-02

This upgrade builds on the already deployed queue and one-shot schedule fixes.

## Changes

- Script Studio and Workflow use the running FastAPI application's control token.
  Filesystem discovery is retained only for standalone router applications.
  A regression test verifies both pages when their disk token is stale.
- Explicit coarse OCR limits recognition samples to four per speech cue.
  Adaptive mode retains its existing coverage and batch limits.
- Translation cache tests use accepted dummy keys and an isolated SQLite circuit
  breaker. Production key validation remains intact.
- Bad-JSON fallback tests select their two models explicitly, isolate breaker/cache,
  mock HTTP, and assert two requests. No paid request is needed.
- Runtime discovery tests cover the first subprocess probe, cached reuse and the
  installed-package fast path independently of the developer's Paddle environment.
- Real audio/model tests support an explicit V1_TEST_ASSETS_ROOT. SenseVoice now
  requires its WAV fixture and actually performs inference and writes an SRT;
  a missing fixture can no longer silently bypass inference.

## Reproduction

Run in the tested worktree, with the existing repo-local Python environments:

```powershell
$env:V1_TEST_ASSETS_ROOT = 'C:\tool v1'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'C:\tool v1\backend\venv\Scripts\python.exe' -B -m pytest -q --tb=line -p no:cacheprovider
& 'C:\tool v1\backend\venv_py310\Scripts\python.exe' -B -m pytest -q --tb=line -p no:cacheprovider
node --test tests/js/*.test.js
```

Assets are read from the configured root, not copied or changed. No dependency
installation, model download, Git reset/merge/push or production job submission
is part of these tests.

## Evidence

- Before: 11 failed, 53 passed, 7 skipped in the affected test files.
- Full Python 3.12: 1284 passed, 80 skipped, 0 failed (142.69 seconds).
- Full Python 3.10: 1284 passed, 80 skipped, 0 failed, 261 subtests passed
  (196.71 seconds).
- JavaScript: 13 passed, 0 failed.
- Offline synthetic render: actual Tool V1 process_video, h264_nvenc, 3.000 seconds,
  valid video/audio streams, full FFmpeg decode passed, 87754-byte output.
- Logs and render evidence are retained under scratch/upgrade-* in the fix worktree.
- Skip inventory: docs/quarantine_inventory_20261102.md. Collection sees 74
  explicit skip markers; the full run has 80 skips including runtime conditions.
  No skip entries were added or removed to achieve the result.

## Remaining acceptance

The synthetic render and real SenseVoice/audio tests are not full AI pipeline
acceptance on representative user videos. Cold/warm/resume video runs, paid-provider
quality checks, performance comparisons and multi-job observation remain open.
The 80 inherited skipped tests require individual review; this upgrade does not
claim that their covered functionality has been verified.

## Deployment and recovery

Only deploy after a fresh idle/empty queue check and exact source comparison.
Retain the previous source; use the verified scoped V1 shutdown with no force,
then resume the original verified Scheduled Tasks. Preserve standalone downloader.
Rollback restores the previous source only after another idle check; current
workspace, queue, schedule and user outputs must remain intact.

## Applied deployment

Applied to C:\tool v1 after fresh idle, empty queue and exact source checks.
The first precheck safely stopped before changes because the task-name comparison
did not normalize the leading Windows task-path slash; this was corrected in
the deployment helper, with task action verification retained.
Scoped shutdown stopped 16 verified V1 processes without force. All five verified
Scheduled Tasks were resumed. Eleven source/test/document files matched the tested
worktree. The previous nine source/test files are preserved at:

`C:\tool_v1_clean\queue-schedule-fix\scratch\before-stability-upgrade-6d2d10fbd1a44353b0fe73414fee6de4`

After reload: health 5/5 PASS, preflight 25/25, Python 3.12.14, pip check clean,
dashboard idle/active=false. Telegram state=running, heartbeat=true, polling=true,
busy=false, queue=0. Both live Studio pages contain the same nonempty server token
and no replacement placeholders. Standalone downloader PID 7568 retained its
2026-11-02 17:06:46 start time. No Git commit or push was performed.
