"""One claimed cloud attempt, with cancellation checks and verified publication."""
import hashlib
import json
import logging
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time

from .cloud_artifacts import probe_media, sha256_file, stage_for_publication, verify_artifact
from .cloud_config import DEFAULT_CONFIG
from .cloud_contracts import (CloudCancelled, CloudCapabilityError, CloudOwnershipError,
                              CloudRunResult, CloudVerificationError, validate_request, redact)
from .cloud_job_store import CloudJobStore, RUNNING
from .colab_bridge import ColabProvider

logger = logging.getLogger('cloud.orchestrator')
GPU_CU_RATES = {'T4': 1.15, 'L4': 1.94, 'A100': 3.85, 'H100': 8, 'G4': 1.5, 'V5E1': 2, 'V6E1': 3.5, 'CPU': .1}


class CloudOrchestrator:
    def __init__(self, store=None, *, provider=None, output_dir=None, probe=probe_media, secret_supplier=None):
        self.store = store or CloudJobStore()
        self.provider = provider or ColabProvider()
        self.output_dir = Path(output_dir or os.getenv('AUTODUB_OUTPUT_DIR') or DEFAULT_CONFIG['results_dir'])
        self.probe = probe
        self.secret_supplier = secret_supplier or (lambda: {'GEMINI_API_KEY': os.getenv('GEMINI_API_KEY', '').strip()})
        self.backend_dir = Path(__file__).resolve().parent.parent
        self.gpu_scripts_dir = Path(__file__).resolve().parent / 'gpu_scripts'

    def process_job(self, job, progress_callback=None, cancel_check=None):
        if not job.attempt_id or not job.owner_id:
            return CloudRunResult('busy', job.job_id, error='cloud_job_not_claimed')
        current = self.store.assert_owned(job.job_id, job.attempt_id, job.owner_id)
        if current.state == 'completed':
            receipt = self.store.get_receipt(job.job_id)
            return CloudRunResult('completed' if receipt else 'recovery_required', job.job_id, job.attempt_id,
                                  current.result_path, receipt, None if receipt else 'cloud_receipt_unverified')
        if current.state not in RUNNING and current.state not in ('cancel_requested', 'stopping'):
            return CloudRunResult(current.state, job.job_id, job.attempt_id, error=current.error)
        # A persisted claim may only start once. Concurrent replays cannot run it.
        if not self.store.start_attempt(job.job_id, job.attempt_id, job.owner_id):
            return CloudRunResult('busy', job.job_id, job.attempt_id, error='cloud_attempt_already_started')

        session = 'cjob' + job.attempt_id
        remote = '/content/cloud-' + job.attempt_id
        attempted_session = False
        secret_ref = None
        error = None
        cleanup_ok = True
        stop_confirmed = False
        staged = None
        secret_values = ()
        started = time.monotonic()

        def observe_cancel():
            if cancel_check is not None:
                try:
                    requested = bool(cancel_check())
                except Exception:
                    requested = True  # Unverifiable local ownership cannot publish.
                if requested:
                    self.store.request_cancel(job.job_id)
            owned = self.store.assert_owned(job.job_id, job.attempt_id, job.owner_id)
            return owned.cancel_requested_at is not None

        def checkpoint():
            if observe_cancel():
                raise CloudCancelled()

        def update(state, percent, step, **kwargs):
            checkpoint()
            current = self.store.assert_owned(job.job_id, job.attempt_id, job.owner_id)
            if not self.store.update_progress(job.job_id, attempt_id=job.attempt_id, owner_id=job.owner_id,
                                              expected_revision=current.revision, state=state,
                                              percent=percent, step_name=step, **kwargs):
                checkpoint()
                raise CloudOwnershipError('stale_cloud_progress')
            if progress_callback:
                try:
                    progress_callback(percent, step)
                except Exception:
                    pass

        def operation(call, *, provisioning=False):
            nonlocal stop_confirmed
            checkpoint()
            done = threading.Event()
            box = {}

            def execute():
                try:
                    box['value'] = call()
                except BaseException as exc:
                    box['error'] = exc
                finally:
                    done.set()

            thread = threading.Thread(target=execute, name='CloudOperation', daemon=True)
            thread.start()
            stop_tried = False
            observation_error = None
            while not done.wait(.1):
                try:
                    cancelled = observe_cancel()
                except Exception as exc:
                    observation_error = exc
                    cancelled = False
                if cancelled and attempted_session and not provisioning and not stop_tried:
                    self.store.begin_stopping(job.job_id, job.attempt_id, job.owner_id)
                    stop_tried = True
                    try:
                        stop_confirmed = bool(self.provider.stop_session(session)[0])
                    except Exception:
                        stop_confirmed = False
            thread.join()  # Drain native work before reporting terminal state.
            if observation_error:
                raise observation_error
            if 'error' in box:
                raise box['error']
            return box['value']

        def checked(call, **kwargs):
            ok, detail = operation(call, **kwargs)
            checkpoint()
            if not ok:
                raise CloudVerificationError('cloud_provider_operation_failed')
            return detail

        try:
            checkpoint()
            self.provider.ensure_ready()  # Before credential access or provisioning.
            validate_request(job.video_path, job.gpu_type, job.task_type, job.voice)
            is_url = job.video_path.startswith(('http://', 'https://'))
            if not is_url and (not Path(job.video_path).is_file() or Path(job.video_path).is_symlink()):
                raise CloudVerificationError('cloud_source_missing')
            source_sha = (hashlib.sha256(job.video_path.encode('utf-8')).hexdigest() if is_url
                          else operation(lambda: sha256_file(job.video_path)))
            if self.store.get_cu_budget_info()['is_exceeded']:
                raise CloudCapabilityError('cloud_daily_budget_exceeded')
            update('creating_session', 10, 'Creating cloud session', session_name=session, source_sha256=source_sha)
            attempted_session = True  # A failed/timeout create may still allocate resources.
            checked(lambda: self.provider.create_session(session, gpu=job.gpu_type), provisioning=True)
            update('uploading', 20, 'Uploading isolated cloud attempt')
            checked(lambda: self.provider.exec_argv(session, ['mkdir', '-p', remote], cwd='/content'))
            with tempfile.TemporaryDirectory(prefix='cloud-bundle-') as temp:
                bundle = Path(temp) / 'cloud_bundle.tar.gz'
                modules = ['ass_utils.py', 'subtitle_layout.py', 'subtitle_text.py', 'v1_ocr_geometry.py',
                           'ocr_subtitle_locator.py', 'v1_timing_solver.py', 'v1_speech_guard.py']
                with tarfile.open(bundle, 'w:gz') as archive:
                    files = list(self.gpu_scripts_dir.glob('*.py')) + list(self.gpu_scripts_dir.glob('*.whl'))
                    files += [self.gpu_scripts_dir / 'requirements_cloud.txt'] + [self.backend_dir / name for name in modules]
                    for path in files:
                        if path.is_file():
                            archive.add(path, arcname=path.name)
                checked(lambda: self.provider.upload_file(session, bundle, remote.lstrip('/') + '/cloud_bundle.tar.gz'))
            checked(lambda: self.provider.exec_argv(session, ['tar', '-xzf', remote + '/cloud_bundle.tar.gz', '-C', remote], cwd=remote))
            input_arg = job.video_path if is_url else remote + '/' + Path(job.video_path).name
            if not is_url:
                checked(lambda: self.provider.upload_file(session, job.video_path, input_arg.lstrip('/')))
                if operation(lambda: sha256_file(job.video_path)) != source_sha:
                    raise CloudVerificationError('cloud_source_changed')
            update('processing', 40, 'Preparing cloud environment')
            checked(lambda: self.provider.exec_argv(session, ['python3', 'setup_env.py'], cwd=remote, timeout=600))
            secret_env = dict(self.secret_supplier())
            secret_values = tuple(secret_env.values())
            if set(secret_env) != {'GEMINI_API_KEY'} or not secret_env['GEMINI_API_KEY']:
                raise CloudCapabilityError('cloud_translation_secret_missing')
            # Provider contract: no value in argv/source/logs; partial-install
            # failures must clean their own temporary credentials.
            secret_ref = operation(lambda: self.provider.install_secret(session, secret_env, job.attempt_id))
            checkpoint()
            secret_env.clear()
            update('processing', 50, 'Running cloud pipeline')
            checked(lambda: self.provider.exec_argv(session, ['python3', 'full_pipeline.py', '--input', input_arg,
                    '--voice', job.voice], cwd=remote, secret_path=secret_ref, timeout=DEFAULT_CONFIG['exec_timeout_seconds']))
            checked(lambda: self.provider.exec_argv(session, ['python3', 'write_output_manifest.py', '--job-id', job.job_id,
                    '--attempt-id', job.attempt_id, '--source-sha256', source_sha,
                    '--source-kind', 'url' if is_url else 'file',
                    '--input', 'downloaded_video.mp4' if is_url else input_arg], cwd=remote))
            update('downloading', 85, 'Downloading cloud output')
            with tempfile.TemporaryDirectory(prefix='cloud-download-') as temp:
                downloaded = checked(lambda: self.provider.download_file(session, remote.lstrip('/') + '/output_video.mp4', temp))
                manifest_path = checked(lambda: self.provider.download_file(session, remote.lstrip('/') + '/output_manifest.json', temp))
                manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
                update('validating', 92, 'Verifying cloud output')
                verification = operation(lambda: verify_artifact(downloaded, manifest, job.job_id, job.attempt_id, source_sha, self.probe))
                checkpoint()
                staged = operation(lambda: stage_for_publication(downloaded, self.output_dir, job.job_id, job.attempt_id, verification))
                checkpoint()
                output = self.output_dir / (DEFAULT_CONFIG['output_prefix'] + job.video_name)
                receipt = self.store.publish(job.job_id, job.attempt_id, job.owner_id, staged, output, verification,
                                             round((time.monotonic()-started)/3600*GPU_CU_RATES[job.gpu_type], 3))
                if not receipt:
                    raise CloudVerificationError('cloud_publication_recovery_required')
        except CloudCancelled:
            error = 'cloud_cancelled'
        except (CloudCapabilityError, CloudOwnershipError, CloudVerificationError) as exc:
            error = redact(exc, secret_values)
        except Exception:
            # Provider/library exception text is not trusted with credentials.
            error = 'cloud_operation_failed'
        finally:
            if secret_ref is not None:
                try:
                    self.provider.cleanup_secret(session, secret_ref)
                except Exception:
                    cleanup_ok = False
            if attempted_session:
                try:
                    if not stop_confirmed:
                        stop_confirmed = bool(self.provider.stop_session(session)[0])
                    cleanup_ok = cleanup_ok and stop_confirmed
                except Exception:
                    cleanup_ok = False
            self.store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, error=error, cleanup_ok=cleanup_ok)
            final_job = self.store.get_job(job.job_id)
            if staged is not None and final_job and not final_job.publication_json:
                staged.unlink(missing_ok=True)
        current = self.store.get_job(job.job_id)
        if not current or (current.attempt_id, current.owner_id) != (job.attempt_id, job.owner_id):
            return CloudRunResult('recovery_required', job.job_id, job.attempt_id, error='stale_cloud_attempt')
        receipt = self.store.get_receipt(job.job_id)
        status = current.state if cleanup_ok else 'stop_incomplete'
        if status == 'completed' and not receipt:
            status = 'recovery_required'
        logger.info('Cloud attempt finished job=%s attempt=%s status=%s', job.job_id, job.attempt_id, status)
        return CloudRunResult(status, job.job_id, job.attempt_id, current.result_path, receipt, error)
