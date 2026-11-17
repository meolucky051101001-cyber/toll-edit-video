"""Bounded, exact OCR reuse for a single video invocation.

Only recognition rows are shared. Time, colour sampling and coordinate mapping
remain the responsibility of each occurrence at the call site.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import hashlib
from pathlib import Path
import time


_active = ContextVar("v1_ocr_observation_ledger", default=None)
_occurrences = ContextVar("v1_ocr_occurrences", default=())


class OCRObservationLedger:
    def __init__(self, source, metrics=None, *, max_bytes=96 * 1024 * 1024, max_entries=512):
        self.source = source
        self.metrics = metrics if metrics is not None else {}
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.observations = OrderedDict()
        self.bytes = 0
        self.serial = 0

    def _add(self, name, amount):
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def recognize(self, frames, recognize, policy, occurrences=()):
        started = time.monotonic()
        prepared = []
        pending = []
        for frame in frames:
            raw = frame.tobytes()
            key = (self.source, policy, tuple(frame.shape), frame.dtype.str,
                   hashlib.sha256(raw).digest())
            hit = None
            for entry_id, entry in list(self.entries.items()):
                if entry[0] == key and entry[1] == raw:
                    hit = entry[2]
                    self.entries.move_to_end(entry_id)
                    break
            if hit is not None:
                prepared.append((key, hit, None))
                continue
            index = next((i for i, item in enumerate(pending)
                          if item[0] == key and item[1] == raw), None)
            if index is None:
                index = len(pending)
                pending.append((key, raw, frame))
            prepared.append((key, None, index))
        self._add("ocr_hash_lookup_seconds", time.monotonic() - started)
        self._add("ocr_requested_frames", len(frames))
        self._add("ocr_unique_inputs", len(pending))
        self._add("ocr_reused_inputs", len(frames) - len(pending))
        rows = []
        if pending:
            started = time.monotonic()
            self._add("ocr_model_batches", 1)
            try:
                rows = recognize([item[2] for item in pending])
            finally:
                self._add("ocr_recognize_batch_seconds", time.monotonic() - started)
            if len(rows) != len(pending):
                raise RuntimeError("OCR returned incomplete unique frames")
            self._add("ocr_successful_inputs", len(pending))
            started = time.monotonic()
            for (key, raw, _), row in zip(pending, rows):
                # Keep the stored representation and recognition rows bounded.
                # Compression of moving imagery can cost as much as inference;
                # use exact raw bytes under a fixed payload budget instead.
                cost = len(raw) + len(repr(row).encode("utf-8"))
                if cost > self.max_bytes:
                    continue
                while self.entries and (self.bytes + cost > self.max_bytes
                                         or len(self.entries) >= self.max_entries):
                    _, old = self.entries.popitem(last=False)
                    self.bytes -= old[3]
                    self._add("ocr_cache_evictions", 1)
                if self.max_entries > 0:
                    self.serial += 1
                    self.entries[self.serial] = (key, raw, deepcopy(row), cost)
                    self.bytes += cost
            self._add("ocr_cache_store_seconds", time.monotonic() - started)
        result = [deepcopy(hit if index is None else rows[index])
                  for _, hit, index in prepared]
        for occurrence, (key, _, _) in zip(occurrences, prepared):
            self.observations[(policy, occurrence)] = key
            self.observations.move_to_end((policy, occurrence))
            while len(self.observations) > 4096:
                self.observations.popitem(last=False)
        self.metrics["ocr_cache_bytes"] = self.bytes
        self.metrics["ocr_cache_peak_bytes"] = max(self.bytes, self.metrics.get("ocr_cache_peak_bytes", 0))
        return result

    def clear(self):
        self.entries.clear()
        self.observations.clear()
        self.bytes = 0
        self.metrics["ocr_cache_bytes"] = 0


@contextmanager
def observation_occurrences(values):
    token = _occurrences.set(tuple(values))
    try:
        yield
    finally:
        _occurrences.reset(token)


def active_ledger():
    return _active.get()


def current_occurrences():
    return _occurrences.get()


def video_observations(fn):
    @wraps(fn)
    def wrapped(video_path, *args, **kwargs):
        path = Path(video_path).resolve()
        try:
            stat = path.stat()
            fingerprint = (str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            fingerprint = (str(path),)
        metrics = kwargs.setdefault("metrics", {})
        ledger = OCRObservationLedger(fingerprint, metrics)
        token = _active.set(ledger)
        try:
            return fn(video_path, *args, **kwargs)
        finally:
            ledger.clear()
            _active.reset(token)
    return wrapped
