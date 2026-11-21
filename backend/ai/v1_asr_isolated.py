"""Run GPU ASR outside the long-lived bot; process exit reclaims native memory."""
import os
import json
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
def extract_subtitles_isolated(audio_path, output_srt_path, original_audio_path=None, *, model_id=None, asr_model=None, chunking=None, chunk_size_s=240.0, overlap_s=0.75, timeout_s=600):
    try:
        import sys
        if 'backend.batch_control' in sys.modules:
            from backend.batch_control import run
        elif 'batch_control' in sys.modules:
            from batch_control import run
        else:
            try:
                from ..batch_control import run
            except (ImportError, ValueError):
                from batch_control import run
    except Exception:
        from batch_control import run
    worker = Path(__file__).resolve().parents[1]/'model_workers'/'v1_asr_worker.py'
    job_options = dict(model_id=model_id or asr_model, chunking=chunking, chunk_size_s=chunk_size_s, overlap_s=overlap_s)
    settings = {'stage': 'asr-v1', 'job_options': job_options, 'worker': fingerprint(worker),
                'recovery': fingerprint(Path(__file__).with_name('v1_conditional_asr.py')),
                'sensevoice': fingerprint(Path(__file__).with_name('v1_sensevoice_adapter.py')),
                'paraformer': fingerprint(Path(__file__).with_name('v1_paraformer_adapter.py')),
                'original_audio': fingerprint(original_audio_path) if original_audio_path and os.path.isfile(original_audio_path) else None,
                'implementation': fingerprint(Path(__file__).with_name('transcription.py')),
                'policy': fingerprint(Path(__file__).with_name('v1_model_policy.py')),
                'env': {k: v for k, v in os.environ.items() if k.startswith(('V1_WHISPER_', 'V1_ASR_'))}}
    from v1_stage_runtime import load_payload, save_payload
    metadata_path = Path(output_srt_path).with_suffix(".asr.json")
    checkpoint = Checkpoint(audio_path, [output_srt_path, str(metadata_path)], settings)
    if checkpoint.hit():
        try:
            segments = load_payload(metadata_path)['segments']
            if segments:
                __import__('logging').getLogger(__name__).info('asr cache hit')
                return segments
        except (ValueError, srt.SRTParseError):
            pass
    python = Path(__file__).resolve().parents[1]/'venv'/'Scripts'/'python.exe'
    if not python.is_file():
        raise RuntimeError('V1 ASR virtual environment is missing')
    # GPU Memory Cooldown & Cache Flush để tránh va chạm bộ nhớ CUDA (status 3221226505)
    if "torch" in sys.modules:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, "ipc_collect"):
                    torch.cuda.ipc_collect()
        except Exception:
            pass
    import gc, time
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        time.sleep(0.15)

    env = dict(os.environ, V1_ASR_REUSE='0', PYTHONIOENCODING='utf-8', V1_ASR_JOB_OPTIONS=json.dumps(job_options))
    warm_env = dict(os.environ)
    warm_env.pop("V1_ASR_JOB_OPTIONS", None)
    warm_enabled = os.getenv("V1_ASR_KEEP_WARM", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    try:
        from ..batch_control import stop_check
    except (ImportError, ValueError):
        try:
            from batch_control import stop_check
        except ImportError:
            stop_check = None
    stop_predicate = stop_check.get() if stop_check is not None else None

    def check_stop():
        if stop_predicate is not None and stop_predicate():
            raise RuntimeError("Batch stop requested")

    with tempfile.TemporaryDirectory(prefix='v1-asr-') as folder:
        result = Path(folder)/'result.srt'
        cmd = [str(python), str(worker), str(Path(audio_path).resolve()), str(result)]
        if original_audio_path and os.path.exists(original_audio_path):
            cmd.append(str(Path(original_audio_path).resolve()))
        creationflags = (
            getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
            getattr(subprocess, 'NORMAL_PRIORITY_CLASS', 0)
        )

        def run_one_shot():
            try:
                run(
                    cmd,
                    check=True,
                    timeout=timeout_s,
                    env=env,
                    cwd=str(worker.parents[1]),
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                )
            except subprocess.CalledProcessError as err:
                if err.returncode not in (3221226505, -1073741819, 0xC0000005):
                    raise
                log = __import__('logging').getLogger(__name__)
                log.warning(
                    "ASR worker crashed with STATUS_ACCESS_VIOLATION (%s). Đang làm sạch VRAM và thử lại...",
                    err.returncode,
                )
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                gc.collect()
                time.sleep(3.0)
                run(
                    cmd,
                    check=True,
                    timeout=timeout_s,
                    env=env,
                    cwd=str(worker.parents[1]),
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                )

        content = None
        segments = None
        if warm_enabled:
            session = None
            try:
                try:
                    from . import v1_asr_session as session
                except ImportError:
                    from ai import v1_asr_session as session
                response = session.run_asr_request(
                    python,
                    worker,
                    {
                        "audio": str(Path(audio_path).resolve()),
                        "output": str(result),
                        "original_audio": (
                            str(Path(original_audio_path).resolve())
                            if original_audio_path and os.path.exists(original_audio_path)
                            else None
                        ),
                        "options": job_options,
                    },
                    timeout=timeout_s,
                    env=warm_env,
                    cwd=str(worker.parents[1]),
                    creationflags=creationflags,
                    check_stop=check_stop if stop_predicate is not None else None,
                )
                if not response.get("success"):
                    raise RuntimeError(response.get("error") or "ASR warm worker returned an error")
                content = result.read_text(encoding='utf-8-sig')
                json_sidecar = result.with_suffix(".json")
                segments = (
                    load_payload(json_sidecar)["segments"]
                    if json_sidecar.is_file()
                    else list(srt.parse(content))
                )
            except Exception as warm_error:
                if stop_predicate is not None and stop_predicate():
                    raise RuntimeError("Batch stop requested") from warm_error
                if session is not None:
                    try:
                        session.close_session()
                    except Exception as close_error:
                        __import__('logging').getLogger(__name__).warning(
                            "Could not close failed ASR warm worker: %s",
                            close_error,
                        )
                __import__('logging').getLogger(__name__).warning(
                    "ASR warm worker failed; falling back to one-shot: %s",
                    warm_error,
                )
                content = None
                segments = None

        if content is None or segments is None:
            run_one_shot()
            content = result.read_text(encoding='utf-8-sig')
            json_sidecar = result.with_suffix(".json")
            segments = (
                load_payload(json_sidecar)["segments"]
                if json_sidecar.is_file()
                else list(srt.parse(content))
            )

        # Publish only completed results; exceptions propagate to the job handler.
        Path(output_srt_path).write_text(content, encoding='utf-8')
        save_payload(metadata_path, segments=segments)
        checkpoint.save()
        return segments
