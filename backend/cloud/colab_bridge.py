"""CLI adapter. Live execution is disabled until CLI/secret transport acceptance.

No environment switch bypasses this gate. The installed CLI's exit-code,
notebook history and secret-file permissions still need independent verification.
"""
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from .cloud_config import DEFAULT_CONFIG, to_wsl_path
from .cloud_contracts import CloudCapabilityError, redact

logger = logging.getLogger("cloud.colab_bridge")
LIVE_GATE_REASON = "cloud_cli_unverified: CLI exit status, cancellation and secret transport require acceptance"


def ensure_live_ready():
    raise CloudCapabilityError(LIVE_GATE_REASON)


def _run_wsl_colab(arguments, timeout=300):
    if not isinstance(arguments, (list, tuple)) or not all(isinstance(a, str) and '\0' not in a for a in arguments):
        raise ValueError("cloud_cli_requires_argv")
    # WSL's -- only ends option parsing; --exec also bypasses the Linux shell.
    # Preserve each caller argument as data across both process boundaries.
    args = ["wsl", "-d", DEFAULT_CONFIG["wsl_distro"], "-u", DEFAULT_CONFIG["wsl_user"], "--exec", "env",
            "PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring", "colab", "--auth", "oauth2", *arguments]
    try:
        process = subprocess.run(args, capture_output=True, timeout=timeout, shell=False)
        return process.returncode, process.stdout.decode('utf-8', errors='replace'), process.stderr.decode('utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return -1, '', 'cloud_cli_timeout'
    except OSError:
        return -2, '', 'cloud_cli_unavailable'


def build_exec_wrapper(argv, cwd, run_id, secret_path=None):
    """Only structured data enters source. A secret value never enters it."""
    if not isinstance(argv, (tuple, list)) or not argv or not all(isinstance(x, str) and '\0' not in x for x in argv):
        raise ValueError('cloud_exec_requires_argv')
    if not isinstance(cwd, str) or '\0' in cwd or not re.fullmatch(r'[a-zA-Z0-9_-]+', run_id):
        raise ValueError('invalid_cloud_exec_context')
    payload = json.dumps(dict(argv=list(argv), cwd=cwd, run_id=run_id, secret_path=secret_path), ensure_ascii=True)
    return (
        "import json, os, subprocess\n"
        f"request = json.loads({payload!r})\n"
        "env = os.environ.copy()\n"
        "secret_path = request.get('secret_path')\n"
        "secrets = []\n"
        "if secret_path:\n"
        "    with open(secret_path, encoding='utf-8') as stream: secret_env = json.load(stream)\n"
        "    os.unlink(secret_path)\n"
        "    if set(secret_env) != {'GEMINI_API_KEY'}: raise ValueError('invalid_secret_schema')\n"
        "    env.update(secret_env)\n"
        "    secrets = list(secret_env.values())\n"
        "try:\n"
        "    result = subprocess.run(request['argv'], cwd=request['cwd'], env=env, shell=False, capture_output=True, text=True)\n"
        "    output = (result.stdout or '') + (result.stderr or '')\n"
        "    for secret in secrets:\n"
        "        if secret: output = output.replace(secret, '[REDACTED]')\n"
        "    print(output)\n"
        "    print('CLOUD_EXEC_RESULT:' + json.dumps({'run_id': request['run_id'], 'returncode': result.returncode}))\n"
        "except Exception:\n"
        "    print('CLOUD_EXEC_RESULT:' + json.dumps({'run_id': request['run_id'], 'returncode': -1}))\n"
    )


def exec_argv(session_name, argv, cwd='/content', timeout=None, secret_path=None):
    ensure_live_ready()
    run_id = uuid.uuid4().hex
    wrapper = build_exec_wrapper(argv, cwd, run_id, secret_path)
    fd, path = tempfile.mkstemp(prefix='cloud-exec-', suffix='.py')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(wrapper)
        logger.info('Cloud operation exec session=%s run=%s', session_name, run_id)
        rc, stdout, _ = _run_wsl_colab(['exec', '-s', session_name, '-f', to_wsl_path(path)], timeout or DEFAULT_CONFIG['exec_timeout_seconds'])
        records = []
        for line in stdout.splitlines():
            if line.startswith('CLOUD_EXEC_RESULT:'):
                try:
                    records.append(json.loads(line.partition(':')[2]))
                except ValueError:
                    pass
        ok = rc == 0 and records == [{'run_id': run_id, 'returncode': 0}]
        # Do not propagate raw provider output: exceptions may contain secrets.
        return ok, '' if ok else 'cloud_remote_execution_failed'
    finally:
        Path(path).unlink(missing_ok=True)


def exec_cmd(*args, **kwargs):
    raise ValueError('cloud_shell_commands_disabled_use_exec_argv')


def exec_script(*args, **kwargs):
    raise ValueError('cloud_unstructured_scripts_disabled_use_exec_argv')


def create_session(session_name, gpu='T4'):
    ensure_live_ready()
    if gpu not in {'T4', 'L4', 'A100', 'H100', 'G4', 'CPU', 'V5E1', 'V6E1'}:
        raise ValueError('invalid_cloud_gpu')
    argv = ['new', '-s', session_name]
    if gpu in {'V5E1', 'V6E1'}:
        argv += ['--tpu', gpu.lower()]
    elif gpu != 'CPU':
        argv += ['--gpu', gpu]
    rc, _, _ = _run_wsl_colab(argv, 120)
    return rc == 0, '' if rc == 0 else 'cloud_provision_failed'


def upload_file(session_name, local_path, remote_filename=None):
    ensure_live_ready()
    path = Path(local_path)
    if not path.is_file() or path.is_symlink():
        return False, 'cloud_upload_source_invalid'
    target = remote_filename or path.name
    target = target if target.startswith('content/') else 'content/' + target.lstrip('/')
    rc, _, _ = _run_wsl_colab(['upload', '-s', session_name, to_wsl_path(path), target], DEFAULT_CONFIG['upload_timeout_seconds'])
    return rc == 0, '' if rc == 0 else 'cloud_upload_failed'


def download_file(session_name, remote_filename, local_dest_dir):
    ensure_live_ready()
    dest = Path(local_dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (Path(remote_filename).name + '.' + uuid.uuid4().hex + '.part')
    remote = remote_filename if remote_filename.startswith('content/') else 'content/' + remote_filename.lstrip('/')
    rc, _, _ = _run_wsl_colab(['download', '-s', session_name, remote, to_wsl_path(output)], DEFAULT_CONFIG['download_timeout_seconds'])
    if rc != 0 or not output.is_file() or output.is_symlink() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        return False, 'cloud_download_failed'
    return True, str(output)


def stop_session(session_name):
    ensure_live_ready()
    rc, _, _ = _run_wsl_colab(['stop', '-s', session_name], DEFAULT_CONFIG['stop_timeout_seconds'])
    # No text substring is proof of termination. No global assignment cleanup.
    return rc == 0, '' if rc == 0 else 'cloud_stop_unconfirmed'


def stop_all_sessions():
    return False, 'cloud_global_stop_disabled_use_owned_job_cancel'


def get_usage():
    return {'ok': False, 'balance': 0.0, 'usage_rate': 0.0, 'active_assignments': 0,
            'error': LIVE_GATE_REASON}


def get_sessions():
    return []


class ColabProvider:
    """No live job can provision while the verification gate is closed."""
    ensure_ready = staticmethod(ensure_live_ready)
    create_session = staticmethod(create_session)
    upload_file = staticmethod(upload_file)
    exec_argv = staticmethod(exec_argv)
    download_file = staticmethod(download_file)
    stop_session = staticmethod(stop_session)

    def install_secret(self, session_name, secret_env, attempt_id):
        # Deliberately no guessed upload/env transport or plaintext fallback.
        raise CloudCapabilityError('cloud_secret_transport_unverified')

    def cleanup_secret(self, session_name, secret_ref):
        raise CloudCapabilityError('cloud_secret_transport_unverified')
