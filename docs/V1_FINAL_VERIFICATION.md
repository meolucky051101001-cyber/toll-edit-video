# V1 improvement round: verification status

2026-09-12. Live source includes the log/memory, voice/OCR and translation updates.
The existing RC3 archive is unchanged and does not include these follow-ups.

## Automated verification

82 selected Python tests passed, with three existing dependency deprecation warnings.
Dashboard JavaScript checks passed. Tests used temporary directories and mocked
providers, without real translation calls or video rendering.

Translation now checks result type/count/nonempty strings and remaining Chinese
text for Vietnamese output. A shared 180-second caller-wait budget and bounded
network calls prevent indefinite waiting. Failed translation restores subtitle
contents. Logs classify provider responses; attempt history and successful models
are separate, with video identity checks to reject stale model writes.

The network worker uses a daemon thread and cannot be forcibly killed safely.
After timeout, at most one outstanding fallback is permitted per module/process.
Late results are not applied to subtitles. Local frame preparation is not forcibly
interrupted by the deadline. This is not a hard remote-request cancellation limit.

## Real-media acceptance remains open

No same-input old/new benchmark, ten-video live soak or listening/visual quality
assessment has been performed for this revision. Ten synthetic sampler lifecycle
tests are not real renders. Do not claim speedup, RAM reduction or zero defects.

Next acceptance: user-approved short/long videos, dense/fast text, music-only and
invalid input. Keep settings fixed; separate cold and warm cache. Record total wall
time, sampled process-tree RSS, system free RAM, attempts, subtitle coverage and
speech timing. Inspect output sound/image and run ten videos sequentially.

Cross-video warm RVC, reduced OCR sample frequency, learned subtitle cropping and
approximate-frame skipping remain gated on quality tests. Existing models, prompts,
provider order, credentials, originals, outputs and V2 were not changed.

See V1_LOG_MEMORY_UPDATE.md and V1_VOICE_OCR_UPDATE.md for implementation details.
