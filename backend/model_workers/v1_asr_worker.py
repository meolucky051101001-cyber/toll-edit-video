"""Single-job V1 ASR worker. No OCR, TTS, Telegram or CPU fallback."""
import sys
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.transcription import extract_subtitles_whisper

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    extract_subtitles_whisper(sys.argv[1], sys.argv[2], num_workers=1)
