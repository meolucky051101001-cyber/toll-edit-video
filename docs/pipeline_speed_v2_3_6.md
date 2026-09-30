# Pipeline 2.3.6: remove redundant speech processing

Measured on the completed 2.3.5 doll job `1788542648_851cf900`:

| Stage | Seconds |
| --- | ---: |
| Separation | 171.2 |
| Transcription | 199.3 |
| OCR | 77.0 |
| Translation | 37.3 |
| Estimated timing rewrite | 86.4 |
| TTS including actual timing rewrite | 227.8 |
| Voice conversion | 150.9 |
| Video render | 40.0 |

The Telegram time is the full job, including model stages, not just video encoding.
This job had 20 source segments and 45 speech segments after timing processing.
Earlier processing of the same source on 2.3.4 had 20 speech segments. Other stage
times also varied, so this comparison does not isolate the cause of all latency.

## Changes

- Keep a fitting speech window together for TTS and RVC. Only split speech when
  needed by the duration solver. Sentence changes remain separate ASS display
  cues, with ellipses removed and measured wrapping within the existing margins.
- On measured-duration rewrites, regenerate only changed, requested segments.
  Preserve the other audio files, batch ordering, source IDs and checkpoint
  restoration. Reject mismatched regenerated audio IDs.
- Local deployment: `ENABLE_PARALLEL_OCR_GEMINI=true`. OCR and translation use
  independent copies and merge OCR geometry into translated segments afterward.
- Local deployment: `GEMINI_MODEL=gemini-3.6-flash`. This is the model that
  succeeded on the observed jobs after configured 3.8 and 3.7 returned HTTP 429.
  Keep existing fallback behavior if the preferred model becomes unavailable.

ASR selection, source resolution, subtitle detection sampling, separator and
voice-conversion model remain unchanged.

## Validation and limits

155 offline tests pass. New regression coverage checks selective regeneration
across two rewrites and cache resume, concurrent OCR/translation with geometry
preserved, and grouped speech with separate sentence display cues.

On the same cached, unshortened doll translation, with remote timing rewrites
disabled for an offline comparison, speech segments drop from 61 to 44 while
61 display cues remain. This is a work-count comparison, not an end-to-end speed
measurement; production uses timing rewrites and will have different counts.
The other sampled translation still requires 24 segments in this comparison.

A local Whisper large-v3 trial on the same doll vocals took 38.63 seconds and
covered 0.00 through 92.94 seconds, compared with the recorded 199.3-second Qwen
stage. However, Whisper returned 75 speech windows versus Qwen's 20; these may
increase downstream TTS/RVC work. Do not change the default ASR based only on
this isolated stage measurement. Qwen succeeded on the doll video and failed
on the subsequent video, where the existing Whisper fallback succeeded.

The final wall-clock gain requires a new complete job. Existing timings cannot
justify a promised speedup ratio. Word-weighted sentence display timing inside
a speech window is approximate, as in the ASS renderer's existing pagination.
