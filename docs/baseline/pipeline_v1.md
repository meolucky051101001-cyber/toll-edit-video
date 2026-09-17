# Pipeline v1 baseline

This document freezes the legacy pipeline before the pipeline v2 refactor.
It is intentionally descriptive: nothing in this directory is imported by the
running Telegram or batch applications.

## Source checkpoint

- Repository: `meobeo240107/auto-dubbing-bot`
- Baseline branch: `main`
- Baseline commit: `a28137d6a966aaf8a34c2d6fa891fe01ff65383f`
- Baseline tag: `baseline-pipeline-v1-a28137d`
- Development branch: `refactor/pipeline-v2`

## Target machine

- Operating system: Windows 11
- GPU: NVIDIA GeForce RTX 4050 Laptop, 6 GB VRAM
- RAM: 16 GB
- Video encoder: FFmpeg H.264 NVENC

Exact Python, CUDA, PyTorch, FFmpeg and model versions must be collected on the
target Windows machine. They are not pinned in the current repository and
cannot be inferred reliably from source code alone.

## Legacy processing flow

1. Telegram/local input or social download.
2. Extract 44.1 kHz stereo audio.
3. Separate vocals with Demucs.
4. Transcribe with Faster-Whisper Large-v3.
5. Detect burned-in subtitle placement with EasyOCR.
6. Translate to Vietnamese with Gemini Flash and video-frame context.
7. Generate Edge TTS audio and optionally run RVC.
8. Generate ASS subtitles and mix audio with Pydub.
9. Render the final video with FFmpeg/NVENC.
10. Return through Telegram or copy to the local output directory.

The flow is currently duplicated across `backend/telegram_bot.py`,
`backend/batch_processor.py` and `backend/main.py`. Pipeline v2 remains detached
from all three entry points during phase 1.

## Compatibility invariants for phases 0 and 1

- Do not import `backend.pipeline_v2` from any legacy entry point.
- Do not change the legacy workspace or output naming.
- Do not change model loading, GPU use, translation, TTS, mixing or rendering.
- Do not require new runtime dependencies for the legacy bot.
- Do not delete or migrate any existing render artifact.
- Keep `PIPELINE_MODE=legacy` as the future rollout default until explicitly
  changed in a later phase.

## Baseline verification

The repository-level safe check is:

```powershell
python -m compileall -q backend
```

A full bot startup/render smoke test requires the target machine's `.env`, AI
models, CUDA stack and media fixtures. Those files are deliberately excluded
from Git. Use `test_video_matrix.json` to record the target-machine results
without committing videos or credentials.
