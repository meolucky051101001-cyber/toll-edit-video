# Tool V1 audio recovery refactor

Scope: existing conditional ASR, OCR gap recovery and adaptive audio mixer.

- Keep normal Whisper settings and reuse its loaded model for gap retries.
- Union ASR intervals before detecting gaps; support nested/unsorted cues.
- Decode original audio once; retry at most 30 seconds of audio in <=8 second
  chunks. Stop scheduling further calls after 45 seconds; this is not a hard
  per-call timeout. The existing isolated worker has its own process timeout.
- Reject recovery results with high no-speech probability, poor average log
  probability, excessive compression ratio or invalid timestamps.
- Include recovery implementation and explicitly supplied original audio in the
  ASR checkpoint fingerprint. Both Telegram paths now supply original audio.
- Keep separate OCR sentences separate; require two distinct observations and
  honor explicit packaging/logo/background classifications.
- Use gain envelopes without overlapping concatenation so music keeps its
  original timing. Measure background level per ducking interval.
- Sum voice tracks in float before PCM conversion to prevent integer overlay
  clipping. Output ceiling is a sample peak, not an EBU loudness or true-peak
  guarantee. Preserve custom ducking settings when overriding gains.
- Missing voice files or mixer failures now fail the job instead of publishing
  a background-only result as a successful dub.

Verification: 34 targeted unit/regression tests passed. No complete video was
rendered and no bot restart was performed. Real noisy-video quality/speed still
needs user acceptance testing. OCR recovery remains heuristic and cannot prove
that unclassified text is dialogue; ASR confidence cannot always distinguish
singing from speech. Retry budgets can leave difficult passages unresolved,
which is logged rather than represented as fully recovered speech.
