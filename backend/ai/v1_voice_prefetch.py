"""Prepare a bounded number of raw CapCut clips while translation is busy."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading


def raw_voice_path(folder, text, voice):
    key = hashlib.sha256(json.dumps(["capcut", voice, text], ensure_ascii=False).encode()).hexdigest()
    return Path(folder) / ".voice-prefetch" / (key + ".mp3")


class CapCutPrefetch:
    def __init__(self, folder, voice, workers, stopped):
        self.folder = folder
        self.voice = voice
        self.stopped = stopped
        self.closed = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=min(2, max(1, workers)))
        self.submitted = set()

    def submit(self, texts):
        from .v1_tech_pronunciation import normalize_text_for_tts
        for text in texts:
            if len(self.submitted) >= 12 or self.stopped() or self.closed.is_set():
                break
            spoken = normalize_text_for_tts(text.strip())
            path = raw_voice_path(self.folder, spoken, self.voice)
            if spoken and path not in self.submitted and not path.exists():
                self.submitted.add(path)
                self.pool.submit(self._prepare, spoken, path)

    def _prepare(self, spoken, path):
        from .voice_cloning import _run_capcut_tts_once, capcut_semaphore
        from .v1_voice_cache import is_valid_audio
        if self.closed.is_set() or self.stopped():
            return
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".mp3", delete=False) as stream:
                tmp = Path(stream.name)
            with capcut_semaphore:
                if self.closed.is_set() or self.stopped():
                    return
                _run_capcut_tts_once(spoken, str(tmp), self.voice, poll_interval=.5, cancel=self.closed)
            if not self.closed.is_set() and not self.stopped() and is_valid_audio(tmp):
                os.replace(tmp, path)
        except Exception as exc:
            logging.getLogger(__name__).debug("Voice prefetch unavailable: %s", type(exc).__name__)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def close(self, wait=True):
        self.closed.set()
        self.pool.shutdown(wait=wait, cancel_futures=True)
