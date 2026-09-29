"""Keep RVC native allocations out of the long-lived Telegram process."""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
import srt

async def generate_dubbing_audio_isolated(segments, output_folder, voice_source='edge', voice_param='vi-VN-HoaiMyNeural', api_key='', video_duration=None, segment_voices=None):
    from .translation import _contains_cjk
    if any(_contains_cjk(s.content) for s in segments):
        raise RuntimeError("Phụ đề chứa ký tự CJK tiếng Trung chưa được dịch (Fail-Closed).")
    try:
        from ..batch_control import run, stop_check
        from .. import shared_state
    except ImportError:
        from batch_control import run, stop_check
        import shared_state
    backend = Path(__file__).resolve().parents[1]
    def work():
        with tempfile.TemporaryDirectory(prefix='v1-voice-') as folder:
            request = Path(folder)/'input.srt'
            result = Path(folder)/'result.json'
            seg_voices_file = Path(folder)/'segment_voices.json'
            # OCR attaches geometry to Subtitle objects. srt.compose's default
            # cloning rejects those extra fields and also renumbers cue IDs.
            # Voice needs only timing/text; leave original OCR objects untouched.
            voice_segments = [srt.Subtitle(s.index, s.start, s.end, s.content,
                                          proprietary=s.proprietary) for s in segments]
            request.write_text(srt.compose(voice_segments, reindex=False), encoding='utf-8')

            # Map segment voices if provided or present on segment objects
            final_segment_voices = dict(segment_voices or {})
            for s in segments:
                s_idx = str(getattr(s, "index", ""))
                if s_idx and s_idx not in final_segment_voices:
                    s_src = getattr(s, "voice_source", None)
                    s_prm = getattr(s, "voice_param", None)
                    if s_src and s_prm:
                        final_segment_voices[s_idx] = {"source": s_src, "param": str(s_prm)}

            if final_segment_voices:
                seg_voices_file.write_text(json.dumps(final_segment_voices, ensure_ascii=False), encoding='utf-8')

            worker_env = dict(os.environ, PYTHONIOENCODING='utf-8')
            if api_key:
                worker_env['VOICE_API_KEY'] = str(api_key)
                worker_env['FPT_API_KEY'] = str(api_key)
            cmd = [
                str(backend/'venv/Scripts/python.exe'), str(backend/'model_workers/v1_voice_worker.py'),
                str(request), str(result), str(Path(output_folder).resolve()), voice_source, voice_param
            ]
            if video_duration is not None:
                cmd.append(str(video_duration))
            else:
                cmd.append("")
            if final_segment_voices:
                cmd.append(str(seg_voices_file))
            try:
                run(cmd,
                    timeout=1800, check=True, cwd=str(backend),
                    env=worker_env,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'NORMAL_PRIORITY_CLASS',0),
                    stdout=subprocess.DEVNULL)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Voice worker subprocess failed with exit code {e.returncode}") from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"Voice worker subprocess timed out after {e.timeout}s") from e

            if not result.is_file():
                raise RuntimeError("Voice worker subprocess failed to produce result file")
            try:
                data = json.loads(result.read_text(encoding='utf-8'))
            except Exception as e:
                raise RuntimeError(f"Voice worker output could not be parsed: {e}") from e
            if not isinstance(data, list):
                raise RuntimeError(f"Voice worker returned invalid data type: {type(data)}")

            # Đồng bộ lại nội dung phụ đề đã rút gọn (nếu có câu dài được rút gọn bởi Gemini)
            for dub in data:
                d_idx = dub.get("index")
                d_content = dub.get("content")
                if d_idx is not None and d_content:
                    for s in segments:
                        if getattr(s, "index", None) == d_idx:
                            s.content = d_content
                            break

            return data
    cancelled = __import__('threading').Event()
    inherited = stop_check.get()
    token = stop_check.set(lambda: cancelled.is_set() or bool(shared_state.stop_requested) or bool(inherited and inherited()))
    try:
        return await asyncio.to_thread(work)
    except asyncio.CancelledError:
        cancelled.set()
        raise
    finally:
        stop_check.reset(token)
