# V2 2.4.0 model speed profile

Set `MODEL_SPEED_PROFILE=fast` for the user-selected default: V1's faster local
models with V2's subtitle detection, rendering, timing, RVC and quality gates.
Use `MODEL_SPEED_PROFILE=quality` to restore the previous model defaults.
Explicit `ASR_BACKEND`, `SOURCE_SEPARATOR_BACKEND`, `DEMUCS_MODEL` and related
model settings still override profile defaults.

| Setting | Fast | Quality |
| --- | --- | --- |
| Primary ASR | Whisper large-v3, word timestamps | Qwen ASR + forced aligner |
| Separation | htdemucs, shifts 0, overlap 0.1 | BS-RoFormer, original model settings |
| Separator fallback | htdemucs_ft | htdemucs_ft, then htdemucs |
| OCR | Existing PP-OCRv6 / EasyOCR fallback | Same |
| Voice | Existing selected RVC model and index | Same |

Fast Whisper windows group adjacent recognized lines only if the gap is at most
0.2 seconds, the combined span at most 4.8 seconds and text at most 48 visible
characters. Words and outer timestamps are retained; significant pauses and
overlaps remain separate. Original Whisper fragment merging is unchanged in
the quality profile. Sentence display changes are still handled in ASS.

Translation receives each segment's time and character budget in its initial
request, across Gemini, OpenAI and DeepSeek. The prompt explicitly preserves
meaning, names, quantities and negation rather than truncating to a budget.
Existing estimated and measured-duration correction remains as a fallback;
only changed audio is regenerated, as in 2.3.6.

Model profile participates in manifest fingerprints. Changing profiles cannot
silently reuse incompatible ASR/separator output. Existing subtitle features
remain: source-matched Chinese detection, compact card, measured wide wrapping,
ellipsis cleanup and separate sentence cues. Audio mixing and QC are unchanged.

## Local measurement

Same cached 94-second doll video previously processed as job
`1788542648_851cf900`, using the installed production Python and models:

| Stage | Previous recorded 2.3.5 job | Fast local trial |
| --- | ---: | ---: |
| Separation | 171.2 s | 18.38 s |
| ASR | 199.3 s | 36.71 s |
| Combined | 370.5 s | 55.09 s |

The fast ASR produced 26 windows, covering 0.00 to 92.94 seconds. The earlier
ungrouped Whisper trial produced 75 windows. This comparison is for local model
stages, across separate runs; it is not a complete render benchmark or a promise
that every video finishes in the same time as V1.

Choosing faster ASR/separation models can change transcription and separation
quality on difficult audio. Keeping the processing features does not imply
identical model output. The quality profile remains available for those clips.

The local OCR verification ran the unchanged 24-sample detector against the
new ASR windows in 75.1 seconds. The selected band was y=0.7375..0.80625,
compared with y=0.75234..0.80625 in the previous job (different sample times).
