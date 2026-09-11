import asyncio
import json
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import srt
from ai.voice_cloning import generate_dubbing_audio

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    segments = list(srt.parse(Path(sys.argv[1]).read_text(encoding='utf-8')))
    result = asyncio.run(generate_dubbing_audio(segments, sys.argv[3], sys.argv[4], sys.argv[5]))
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
