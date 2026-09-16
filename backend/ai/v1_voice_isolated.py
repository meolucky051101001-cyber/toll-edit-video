"""Keep RVC native allocations out of the long-lived Telegram process."""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
import srt

async def generate_dubbing_audio_isolated(segments, output_folder, voice_source='edge', voice_param='vi-VN-HoaiMyNeural', api_key=''):
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
            # OCR attaches geometry to Subtitle objects. srt.compose's default
            # cloning rejects those extra fields and also renumbers cue IDs.
            # Voice needs only timing/text; leave original OCR objects untouched.
            voice_segments = [srt.Subtitle(s.index, s.start, s.end, s.content,
                                          proprietary=s.proprietary) for s in segments]
            request.write_text(srt.compose(voice_segments, reindex=False), encoding='utf-8')
            worker_env = dict(os.environ, PYTHONIOENCODING='utf-8')
            if api_key:
                worker_env['VOICE_API_KEY'] = str(api_key)
                worker_env['FPT_API_KEY'] = str(api_key)
            try:
                run([str(backend/'venv/Scripts/python.exe'), str(backend/'model_workers/v1_voice_worker.py'),
                     str(request), str(result), str(Path(output_folder).resolve()), voice_source, voice_param],
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
