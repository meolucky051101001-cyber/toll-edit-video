"""Bounded V1 telemetry and admission control; never kills unrelated processes."""
import asyncio
import contextvars
import logging
import math
import os
import threading
import time

log = logging.getLogger('v1.performance')
video_context = contextvars.ContextVar('v1_video_metrics', default=None)


def execution_admission(settings=None, memory=None, vram=None):
    """Return a new-work ceiling, without interrupting executions already admitted.

    Telemetry may be injected for deterministic tests. The one-worker ceiling
    remains available when a second execution cannot be measured safely.
    """
    from worker_settings import experimental_dual_workers_enabled, get_worker_settings
    result = {"capacity": 1, "reason": "single_worker", "telemetry_available": False}
    if not experimental_dual_workers_enabled():
        result["reason"] = "experimental_dual_workers_disabled"
        return result
    try:
        settings = dict(settings if settings is not None else get_worker_settings())
        requested = settings.get("effective_concurrency", settings.get("concurrency", 1))
        if type(requested) is not int or requested not in (1, 2):
            raise ValueError("Invalid worker capacity")
        if requested < 2 or settings.get("auto_throttled"):
            result["reason"] = settings.get("throttle_reason") or "single_worker_requested"
            return result
        minimum_vram = float(settings.get("min_free_vram_mb", 1500.0))
        maximum_percent = float(settings.get("max_vram_percent_threshold", 85.0))
        if not math.isfinite(minimum_vram) or minimum_vram < 0 or not math.isfinite(maximum_percent) or not 0 < maximum_percent <= 100:
            raise ValueError("Invalid resource thresholds")
        memory = memory_snapshot() if memory is None else memory
        if vram is None:
            from v1_vram_monitor import get_vram_stats
            vram = get_vram_stats(force_refresh=True)
        if not isinstance(memory, dict) or not isinstance(vram, dict):
            raise ValueError("Invalid resource telemetry")
        available = memory.get("available_mb")
        numbers = (available, vram.get("free_mb"), vram.get("total_mb"), vram.get("percent"), vram.get("timestamp"))
        if (not vram.get("available") or any(type(value) not in (int, float) or not math.isfinite(value) for value in numbers)
                or not 0 <= vram["free_mb"] <= vram["total_mb"] or vram["total_mb"] <= 0
                or not 0 <= vram["percent"] <= 100 or not -1 <= time.time() - vram["timestamp"] <= 5):
            result["reason"] = "resource_telemetry_unavailable"
            return result
        result["telemetry_available"] = True
        if available < 4096:
            result["reason"] = "ram_below_second_worker_reserve"
        elif vram["free_mb"] < max(1500.0, minimum_vram):
            result["reason"] = "vram_below_second_worker_reserve"
        elif vram["percent"] >= min(85.0, maximum_percent):
            result["reason"] = "vram_pressure"
        elif vram.get("auto_throttled"):
            result["reason"] = "vram_throttled"
        else:
            result.update(capacity=2, reason="resources_available")
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, ImportError):
        result["reason"] = "resource_telemetry_unavailable"
    return result


def memory_snapshot():
    """RSS sum can double-count shared pages; this is not unique physical RAM."""
    try:
        import psutil
        root = psutil.Process()
        rss, count = 0, 0
        for process in [root] + root.children(recursive=True):
            try:
                rss += process.memory_info().rss
                count += 1
            except psutil.Error:
                pass
        return dict(tree_rss_mb=round(rss / 1048576, 1), process_count=count,
                    available_mb=round(psutil.virtual_memory().available / 1048576, 1))
    except Exception:
        return dict(tree_rss_mb=None, process_count=None, available_mb=None)


class VideoMetrics:
    def __init__(self, video_id):
        self.video_id = video_id
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.peak = None
        self.minimum = None
        self.samples = 0
        self.thread = threading.Thread(target=self._sample, daemon=True, name='v1-memory-sampler')
        self.thread.start()

    def _sample(self):
        while not self.stop.is_set():
            data = memory_snapshot()
            if data['tree_rss_mb'] is not None:
                self.peak = max(self.peak or 0, data['tree_rss_mb'])
                self.minimum = min(self.minimum if self.minimum is not None else float('inf'), data['available_mb'])
                self.samples += 1
            self.stop.wait(1)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=2)
        return dict(video_id=self.video_id, wall_seconds=round(time.monotonic()-self.started, 2),
                    sampled_tree_rss_peak_mb=self.peak, sampled_system_available_min_mb=self.minimum,
                    samples=self.samples, sample_interval_seconds=1,
                    memory_scope='worker_process_tree_rss_sum_not_unique_ram')


def begin_video(video_id):
    previous = video_context.get()
    if previous:
        previous.finish()
    video_context.set(VideoMetrics(video_id))


def end_video():
    metrics = video_context.get()
    if metrics is None:
        return None
    video_context.set(None)
    return metrics.finish()


def current_video_id():
    metrics = video_context.get()
    return metrics.video_id if metrics else os.environ.get('V1_METRICS_VIDEO_ID', 'untracked')


def speech_parallelism():
    available = memory_snapshot()['available_mb']
    # Never increase the previous ceiling of four cues. Unknown memory is conservative.
    return 1 if available is None or available < 2048 else 2 if available < 4096 else 4


async def bounded_speech_map(function, items):
    """Rolling bounded admission: refill free slots without waiting for a whole wave."""
    iterator, results, pending = iter(items), [], {}
    previous_limit = None
    exhausted = False
    try:
        while True:
            limit = speech_parallelism()
            if limit != previous_limit:
                log.info('video_id=%s speech_parallelism=%s (RAM-aware cue admission)', current_video_id(), limit)
                previous_limit = limit
            while not exhausted and len(pending) < limit:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                index = len(results)
                results.append(None)
                pending[asyncio.create_task(function(item))] = index
            if not pending:
                return results
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                index = pending.pop(task)
                try:
                    results[index] = task.result()
                except (Exception, asyncio.CancelledError) as error:
                    results[index] = error
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def ocr_batch_limit(frame_bytes):
    """Bound raw frame storage; model memory is separately controlled by admission."""
    available = memory_snapshot()['available_mb']
    budget_mb = 8 if available is None or available < 2048 else 24 if available < 4096 else 48
    return max(1, min(12, int(budget_mb * 1048576 // max(1, frame_bytes))))


def wait_for_memory(timeout=20):
    """Pause only new heavy-stage admission, bounded and cooperatively cancellable."""
    from batch_control import stop_check
    started = time.monotonic()
    warned = False
    while True:
        predicate = stop_check.get()
        if predicate and predicate():
            raise RuntimeError('Batch stop requested')
        available = memory_snapshot()['available_mb']
        if available is None or available >= 768:
            return time.monotonic() - started
        if not warned:
            log.warning('video_id=%s RAM thấp: còn %.0f MB; chờ tối đa %ss trước khi nạp bước nặng.',
                        current_video_id(), available, timeout)
            warned = True
        if time.monotonic() - started >= timeout:
            raise RuntimeError('RAM khả dụng dưới 768 MB; chưa nạp bước xử lý mới. Hãy giải phóng RAM rồi thử lại.')
        time.sleep(0.25)
