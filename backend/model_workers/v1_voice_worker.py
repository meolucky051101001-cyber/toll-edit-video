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
    video_duration = None
    if len(sys.argv) > 6 and sys.argv[6] not in ("", "None", "null"):
        try:
            video_duration = float(sys.argv[6])
        except (ValueError, TypeError):
            video_duration = None

    segments = list(srt.parse(input_srt.read_text(encoding='utf-8')))

    if len(sys.argv) > 7 and sys.argv[7] not in ("", "None", "null"):
        p = Path(sys.argv[7])
        if p.is_file():
            try:
                segment_voices_map = json.loads(p.read_text(encoding='utf-8'))
                for seg in segments:
                    idx_str = str(getattr(seg, "index", ""))
                    if idx_str in segment_voices_map:
                        info = segment_voices_map[idx_str]
                        seg.voice_source = info.get("source")
                        seg.voice_param = info.get("param")
                        seg.voice_id = info.get("id")
            except Exception as e:
                logging.warning(f"Error applying segment_voices_map: {e}")

    result = asyncio.run(generate_dubbing_audio(segments, output_folder, voice_source, voice_param, api_key=api_key, video_duration=video_duration))
    output_json.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
