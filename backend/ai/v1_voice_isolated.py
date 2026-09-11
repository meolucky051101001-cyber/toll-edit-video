"""Keep RVC native allocations out of the long-lived Telegram process."""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
import srt

async def generate_dubbing_audio_isolated(segments, output_folder, voice_source='edge', voice_param='vi-VN-HoaiMyNeural', api_key=''):
    # Preserve in-memory repair semantics for legacy untranslated input.
    from .translation import _contains_cjk
    if api_key or any(_contains_cjk(s.content) for s in segments):
        from .voice_cloning import generate_dubbing_audio
        return await generate_dubbing_audio(segments, output_folder, voice_source, voice_param, api_key)
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
            run([str(backend/'venv/Scripts/python.exe'), str(backend/'model_workers/v1_voice_worker.py'),
                 str(request), str(result), str(Path(output_folder).resolve()), voice_source, voice_param],
                timeout=1800, check=True, cwd=str(backend),
                env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'NORMAL_PRIORITY_CLASS',0),
                stdout=subprocess.DEVNULL)
            return json.loads(result.read_text(encoding='utf-8'))
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
