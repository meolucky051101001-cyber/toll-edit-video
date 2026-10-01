"""Single-job V1 ASR worker. No OCR, TTS, Telegram or CPU fallback."""
import sys
import os
import json
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.transcription import extract_subtitles_whisper

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    orig_audio = sys.argv[3] if len(sys.argv) > 3 else None
    options = json.loads(os.environ.get("V1_ASR_JOB_OPTIONS", "{}"))
    segments = extract_subtitles_whisper(sys.argv[1], sys.argv[2], num_workers=1, original_audio_path=orig_audio, **options)
    from v1_stage_runtime import save_payload
    save_payload(Path(sys.argv[2]).with_suffix(".json"), segments=segments)
