"""One leased ASR model; park on CPU between jobs and evict under pressure."""
import atexit
import logging
import os
import threading
import time
from contextlib import contextmanager

class ModelCache:
    def __init__(self, memory_ok=None, ttl=600, check_interval=10):
        self.lock = threading.RLock()
        self.model = self.key = self.timer = None
        self.parked = False
        self.memory_ok = memory_ok or self._memory_ok
        self.ttl, self.check_interval = ttl, check_interval
        self.idle_since = 0

    @staticmethod
    def _memory_ok():
        try:
            import psutil
            return psutil.virtual_memory().available >= 4 * 1024**3
        except Exception:
            return False

    def _clear(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.model is not None:
            try:
                self.model.model.unload_model()
            except Exception:
                logging.getLogger(__name__).warning("ASR cache unload failed")
        self.model = self.key = None
        self.parked = False

    def close(self):
        with self.lock:
            self._clear()

    def _schedule(self):
        self.timer = threading.Timer(self.check_interval, self._check)
        self.timer.daemon = True
        self.timer.start()

    def _check(self):
        with self.lock:
            self.timer = None
            if self.model is None:
                return
            if time.monotonic() - self.idle_since >= self.ttl or not self.memory_ok():
                self._clear()
                logging.getLogger(__name__).info("ASR cache evicted: idle or memory pressure")
            else:
                self._schedule()

    @contextmanager
    def lease(self, key, factory):
        # Lock covers lazy segment iteration, not just construction.
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            if self.key != key:
                self._clear()
            started = time.monotonic()
            reused = self.model is not None
            try:
                if self.model is None:
                    self.model = factory()
                    self.key = key
                elif self.parked:
                    self.model.model.load_model()
                self.parked = False
                logging.getLogger(__name__).info("ASR acquire reused=%s seconds=%.2f", reused, time.monotonic()-started)
                yield self.model
            except BaseException:
                self._clear()
                raise
            finally:
                if self.model is not None:
                    enabled = os.getenv("V1_ASR_REUSE", "1") != "0"
                    if enabled and self.memory_ok():
                        try:
                            started = time.monotonic()
                            self.model.model.unload_model(to_cpu=True)
                            self.parked = True
                            # Offloading can itself consume the remaining RAM.
                            if not self.memory_ok():
                                self._clear()
                            else:
                                self.idle_since = time.monotonic()
                                self._schedule()
                                logging.getLogger(__name__).info("ASR parked on CPU seconds=%.2f", time.monotonic()-started)
                        except Exception:
                            self._clear()
                    else:
                        self._clear()

cache = ModelCache()
atexit.register(cache.close)
