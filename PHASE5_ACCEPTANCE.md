# Phase 5 acceptance — 2026-09-15

Scope: adaptive OCR acquisition for Tool V2. Pipeline implementation: 2.10.0.

## Implemented

- Replaced fixed sparse adaptive timestamps with one midpoint for very short cues, or 30%/70% coarse anchors, followed by visual verification/refinement.
- Reused the actual coarse inference results instead of recognizing those frames again.
- Used 0.2-second visual checks for short ASR-matched cues; unmatched or long cues refine at up to 0.5-second spacing with a midpoint check. Tracking interpolation uses the corresponding sampling interval.
- Reused recognized rows only when observed image signatures agree. The whole search crop participates so disappearance, a second line, or movement outside the previous band can invalidate reuse.
- Bounded reuse age to 3 seconds for short matched cues and 1 second for long/uncertain cues. Changing frames still require recognition.
- Read timestamps in order with forward frame grabs, seeking at pass/cue boundaries or large gaps rather than at every sample. Each cue uses a coarse pass and a verification pass.
- Bounded pending inference batches to 12 images and released coarse images after each cue. Recognition stays on the existing configured backend.
- Released capture on cancellation, decode failure, and incomplete inference results; these conditions raise instead of pretending verification succeeded.
- Persisted sampling metrics in `ocr/result.json` for both isolated worker and in-process paths: coarse/refinement/recognized frames, reuse, seeks, decoded samples, batch peak, intervals, elapsed acquisition time.
- Invalidated older cached pipeline artifacts by advancing implementation version from 2.9.0 to 2.10.0.

## Evidence

- Full unittest suite: 296 tests passed in 48.090 seconds, exit 0.
- Nine new tests exercise adaptive acquisition, delayed text outside coarse anchors, absence, vertical movement, long cues, cancellation, decode failures, incomplete OCR, single-frame input, and production tracking continuity.
- Stable timed fixture: 6 seconds / 3 cues require 6 recognized frames, no additional refinement inference, more than 20 visually reused samples, and at most 6 seeks.
- Real encoded 24fps MJPEG fixture verifies decoder timestamps, a blank subtitle interval, and return at a new vertical position. OCR inference is mocked in this test.
- Existing locator, cover, tracking, provider, queue and QC tests remain green.
- Tool V1 baseline verifier matched the expected commit/tree and 267 Git blobs. This is committed-object verification, not a claim about every working-tree file.
- ENABLE_ADAPTIVE_OCR=true is already configured. Telegram Bot V2 was not started; only its dashboard remains running.

## Practical limits

This is not a live-model/end-to-end speed benchmark. Moving backgrounds or uncertain text can invalidate reuse and require more inference. Sampling at 0.2/0.5 seconds is not exhaustive per-frame detection and may miss shorter transients. Full-video model latency, GPU memory and actual render speed remain for the planned real benchmark stage. No claim is made that every video will finish in a fixed time.

Release manifest and a hashed source backup are generated after the tests. Tool V1 files, dependencies and processes were not changed.
