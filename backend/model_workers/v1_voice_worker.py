import asyncio
import json
import logging
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import srt
from ai.voice_cloning import generate_dubbing_audio

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    input_srt = Path(sys.argv[1])
    output_json = Path(sys.argv[2])
    output_folder = sys.argv[3]
    voice_source = sys.argv[4]
    voice_param = sys.argv[5]

    api_key = os.environ.get("VOICE_API_KEY") or os.environ.get("FPT_API_KEY", "")
    segments = list(srt.parse(input_srt.read_text(encoding='utf-8')))
    result = asyncio.run(generate_dubbing_audio(segments, output_folder, voice_source, voice_param, api_key=api_key))
    output_json.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
