"""One isolated OCR process per job; retains the model between bounded batches."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from .model_runtime import ModelRuntimeError, _creation_flags, _worker_path


class OCRSession:
    def __init__(self, policy):
        self.temp = tempfile.TemporaryDirectory(prefix='v2-ocr-session-')
        self.root = Path(self.temp.name)
        self.number = 0
        cache = Path(policy.model_cache_directory).resolve()
        env = dict(os.environ, HF_HOME=str(cache/'huggingface'),
                   MODELSCOPE_CACHE=str(cache/'modelscope'), PADDLE_PDX_CACHE_HOME=str(cache/'paddlex'),
                   PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        self.log = (self.root/'worker.log').open('wb')
        try:
            self.process = subprocess.Popen([str(policy.runtime_python_path()), str(_worker_path()),
                '--session-dir', str(self.root)], env=env, stdin=subprocess.DEVNULL,
                stdout=self.log, stderr=self.log, creationflags=_creation_flags())
        except BaseException:
            self.log.close()
            self.temp.cleanup()
            raise

    def run(self, payload, timeout):
        self.number += 1
        request = self.root/('request-%06d.json' % self.number)
        response = self.root/('response-%06d.json' % self.number)
        temporary = request.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        os.replace(temporary, request)
        deadline = time.monotonic()+timeout
        while not response.exists():
            if self.process.poll() is not None:
                raise ModelRuntimeError('OCR session exited before returning a response')
            if time.monotonic() >= deadline:
                self.close()
                raise ModelRuntimeError('OCR session timed out')
            time.sleep(.05)
        data = json.loads(response.read_text(encoding='utf-8'))
        response.unlink()
        if not data.get('success'):
            raise ModelRuntimeError(data.get('error', 'OCR failed'))
        return data['result']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
        self.temp.cleanup()
