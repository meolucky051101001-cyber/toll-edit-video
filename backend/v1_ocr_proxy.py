"""Bounded, reusable OCR input; never transcode with a CPU video encoder."""
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time

try:
    from .batch_control import run
    from .v1_media_streams import probe_main_video, validate_video_output
except ImportError:
    from batch_control import run
    from v1_media_streams import probe_main_video, validate_video_output

logger = logging.getLogger(__name__)
CACHE_VERSION = "timed-stream-cuda-v2"


def proxy_size(source):
    # Ordinary 720p/1080p needs no additional lossy conversion. Keep at least
    # 720 pixels on the short side for readable text in unusually tall videos.
    if source.width * source.height <= 1920 * 1080:
        return source.width, source.height
    ratio = min(1.0, max(1280.0 / max(source.width, source.height),
                         720.0 / min(source.width, source.height)))
    return (max(2, int(source.width * ratio) // 2 * 2),
            max(2, int(source.height * ratio) // 2 * 2))


def preparation_budget(duration):
    return min(180.0, max(60.0, 30.0 + max(0.0, duration) * 0.5))


def _cached(path, source, size):
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        validate_video_output(path, source, expected_size=size)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        # Preserve an old artifact until a successful atomic replacement is ready.
        return False


def _prepare(video_path, source, cache_root=None):
    path = Path(video_path).resolve()
    stat = path.stat()
    size = proxy_size(source)
    key = hashlib.sha256(json.dumps([
        CACHE_VERSION, str(path), stat.st_size, stat.st_mtime_ns,
        source.index, source.width, source.height, source.duration, size,
    ], ensure_ascii=True).encode("utf-8")).hexdigest()[:24]
    cache = Path(cache_root) if cache_root is not None else path.parent / ".v1_ocr_cache"
    cache.mkdir(parents=True, exist_ok=True)
    proxy = cache / (key + ".proxy.mp4")
    stream_copy = cache / (key + ".stream.mp4")
    for cached, dimensions in ((proxy, size), (stream_copy, (source.width, source.height))):
        if _cached(cached, source, dimensions):
            logger.info("OCR input cache hit stream=%s file=%s", source.index, cached.name)
            return cached

    deadline = time.monotonic() + preparation_budget(source.duration)
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=str(cache)) as directory:
        temporary = Path(directory) / "ocr.mp4"
        prefix = ["ffmpeg", "-v", "error", "-nostats", "-y", "-threads", "4",
                  "-filter_threads", "2", "-i", str(path), "-map", source.input_map,
                  "-an", "-sn", "-dn"]
        length = ["-t", f"{source.duration:.6f}"] if source.duration > 0 else []
        needs_resize = size != (source.width, source.height)
        if needs_resize:
            gpu_prefix = list(prefix)
            input_position = gpu_prefix.index("-i")
            gpu_prefix[input_position:input_position] = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            command = gpu_prefix + ["-vf", f"scale_cuda={size[0]}:{size[1]}:format=yuv420p", "-c:v", "h264_nvenc",
                                "-preset", "p1", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                                "-g", "12", "-fps_mode", "passthrough"] + length + [str(temporary)]
            try:
                run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=max(1.0, deadline - time.monotonic() - 10.0))
                validate_video_output(temporary, source, expected_size=size)
                os.replace(temporary, proxy)
                logger.info("OCR GPU proxy ready source=%dx%d proxy=%dx%d stream=%s; FPS/timeline preserved",
                            source.width, source.height, *size, source.index)
                return proxy
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                logger.warning("OCR GPU proxy unavailable (%s): %s; use bounded stream copy, not CPU encoding",
                               type(exc).__name__, detail)

        # Lossless demux fallback excludes cover art, without a CPU frame encoder.
        # The existing OCR inference remains GPU-only.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Hết thời gian chuẩn bị OCR; không chuyển sang encoder CPU.")
        run(prefix + ["-c:v", "copy"] + length + [str(temporary)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=min(15.0, remaining))
        validate_video_output(temporary, source)
        os.replace(temporary, stream_copy)
        logger.info("OCR lossless video-only input ready stream=%s; no frame re-encoding", source.index)
        return stream_copy


def ocr_proxy(fn):
    @functools.wraps(fn)
    def wrapped(video_path, *args, **kwargs):
        source = probe_main_video(video_path)
        if proxy_size(source) == (source.width, source.height) and not source.has_cover and source.video_stream_count == 1:
            return fn(video_path, *args, **kwargs)
        prepared = _prepare(video_path, source)
        blocks, _, _, y = fn(str(prepared), *args, **kwargs)
        # Coordinates are normalised; final rendering always uses the untouched source.
        return blocks, source.width, source.height, y
    return wrapped
