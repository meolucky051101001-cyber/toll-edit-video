# RC3 reviewed status — 2026-09-12

This is a tested candidate, not a production benchmark or a claim of zero bugs.
Historical Phase 0–9 claims in CHATGPT_REVIEW_GUIDE.md are not new test results.

## Implemented

- Release packaging excludes .env variants, patch archives, credentials, non-source
  payloads and external links. It checks candidate files against configured secret
  values without printing those values. Local .env is not changed.
- Direct media API routes claim the same file lease as Telegram/batch.
  Client disconnects do not release that lease while the wrapped task runs.
- API validation retains HTTPException status; unexpected media errors return 500.
- V1 history uses OS locks, not age-based removal. Stable lock files remain on disk.
- A new video's translation model is never borrowed from another video's history.
- No model names, provider ordering or API credentials have been changed by RC3.

## Verification

Result: 3 new safety tests and 29 existing API/hardening tests passed.
Dashboard JavaScript regression checks passed after adapting the test DOM to
the existing token wrapper, scaleX progress bar and log table. No UI change.
The archive passed CRC validation and syntax parsing of 197 Python files.

- tests/test_rc3_safety.py: packaging boundary, actual second-process contention
  for history and API leases, lease retention after client cancellation.
- Existing API/Final Gate/Phase 9 tests were run with a temporary AUTODUB_WORKSPACE.
- No live GPU jobs, API translation requests or bot restarts are part of these tests.

## Remaining deployment and measurement gates

- Restart only after the user's queue is empty. Older running processes retain old
  code. Mixing the old age-based history writer and the new lock writer is unsafe.
- V2 writes the same history but does not adopt V1's new lock protocol. Simultaneous
  V1/V2 history writes are NOT certified; changing V2 requires a separately scoped update.
- Provider fallback policy remains the current code's Gemini/OpenAI/DeepSeek/G4F
  behavior. Do not silently revert it to an older Gemini-only policy.
- Compare baseline and RC3 on identical copied videos in isolated output/workspace
  directories: short speech, long speech, dense Chinese subtitles, music-only and
  broken input. Use the same models and audio settings. Separate cold and warm runs.
- Record total and per-stage seconds, peak RAM/VRAM, translation requests/cache
  hits, failures, subtitle alignment and final audio/video inspection. Then perform
  a sequential 10-video soak. No speedup percentage is claimed until measured.
- Cache keys/credentials may influence cache hits; do not mix a warm RC3 result with
  a cold baseline or change provider/model to claim a speed improvement.
- The old RC2 zip contains .env and must not be distributed. It is preserved locally
  for recovery, not cleaned in place. Replace keys if that archive was exposed.

## Scope

Production models, video originals, bot queue and V2 source remain unchanged.
The standalone package has no models or environment; it is not a one-click fully
configured copy of this machine.
