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
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
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
        if not p.is_file():
            raise RuntimeError('Per-cue voice map is missing; refusing wrong-voice fallback')
        segment_voices_map = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(segment_voices_map, dict):
            raise ValueError('Per-cue voice map must be an object')
        cue_ids = [str(seg.index) for seg in segments]
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError('Duplicate subtitle cue IDs; cannot apply per-cue voices')
        for seg in segments:
            info = segment_voices_map.get(str(seg.index))
            if not isinstance(info, dict) or not info.get('source') or not info.get('param'):
                raise RuntimeError(f'Per-cue voice missing for #{seg.index}; refusing silent fallback')
            seg.voice_source = info['source']
            seg.voice_param = info['param']
            seg.voice_id = info.get('id')
            seg.gender = info.get('gender', 'unknown')

    script_mode = os.environ.get("V1_SCRIPT_MODE", "default")
    try:
        result = asyncio.run(generate_dubbing_audio(segments, output_folder, voice_source, voice_param, api_key=api_key, video_duration=video_duration, script_mode=script_mode))
    except Exception as exc:
        from v1_retry_policy import should_retry_job
        from ai.v1_gemini_dispatcher import mask_secret
        output_json.write_text(json.dumps({'error': mask_secret(str(exc)), 'retryable': should_retry_job(exc)}, ensure_ascii=False), encoding='utf-8')
        logging.error("Voice worker failed: %s", mask_secret(str(exc)))
        sys.exit(1)
    for r in result:
        if isinstance(r, dict) and "duration" not in r:
            r["duration"] = r.get("actual_audio_duration", 0.0)
    output_json.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
