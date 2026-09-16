"""Run GPU ASR outside the long-lived bot; process exit reclaims native memory."""
import os
import sys
import subprocess
import tempfile
from pathlib import Path
import srt
try:
    from ..v1_checkpoint import Checkpoint, fingerprint
except ImportError:
    from v1_checkpoint import Checkpoint, fingerprint
try:
    from ..v1_stage_metrics import stage
except ImportError:
    from v1_stage_metrics import stage

@stage("asr_worker_total")
def extract_subtitles_isolated(audio_path, output_srt_path):
    try:
        from ..batch_control import run
    except ImportError:
        from batch_control import run
    worker = Path(__file__).resolve().parents[1]/'model_workers'/'v1_asr_worker.py'
    settings = {'stage': 'asr-v1', 'worker': fingerprint(worker),
                'implementation': fingerprint(Path(__file__).with_name('transcription.py')),
                'policy': fingerprint(Path(__file__).with_name('v1_model_policy.py')),
                'env': {k: v for k, v in os.environ.items() if k.startswith(('V1_WHISPER_', 'V1_ASR_'))}}
    checkpoint = Checkpoint(audio_path, [output_srt_path], settings)
    if checkpoint.hit():
        try:
            segments = list(srt.parse(Path(output_srt_path).read_text(encoding='utf-8-sig')))
            if segments:
                __import__('logging').getLogger(__name__).info('asr cache hit')
                return segments
        except (ValueError, srt.SRTParseError):
            pass
    python = Path(__file__).resolve().parents[1]/'venv'/'Scripts'/'python.exe'
    if not python.is_file():
        raise RuntimeError('V1 ASR virtual environment is missing')
    env = dict(os.environ, V1_ASR_REUSE='0', PYTHONIOENCODING='utf-8')
    with tempfile.TemporaryDirectory(prefix='v1-asr-') as folder:
        result = Path(folder)/'result.srt'
        run([str(python), str(worker), str(Path(audio_path).resolve()), str(result)],
            check=True, timeout=600, env=env, cwd=str(worker.parents[1]),
            creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
                           getattr(subprocess, 'NORMAL_PRIORITY_CLASS', 0)),
            stdout=subprocess.DEVNULL, stderr=None)
        content = result.read_text(encoding='utf-8-sig')
        segments = list(srt.parse(content))
        # Publish only completed results; exceptions propagate to the job handler.
        Path(output_srt_path).write_text(content, encoding='utf-8')
        checkpoint.save()
        return segments
