"""Select the timed video, not MP4 cover art, for OCR and final rendering."""
from dataclasses import dataclass
import json
import math
import subprocess

try:
    from .batch_control import run
except ImportError:
    from batch_control import run


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rate(value):
    numerator, separator, denominator = str(value).partition("/")
    return _number(numerator) / _number(denominator) if separator and _number(denominator) else _number(value)


@dataclass(frozen=True)
class MainVideo:
    index: int
    width: int
    height: int
    fps: float
    duration: float
    video_stream_count: int
    audio_stream_count: int
    has_cover: bool

    @property
    def input_map(self):
        return f"0:{self.index}"


def select_main_video(data):
    streams = data.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    candidates = [stream for stream in videos
                  if not _number(stream.get("disposition", {}).get("attached_pic", 0))
                  and _number(stream.get("width")) > 0 and _number(stream.get("height")) > 0]
    if not candidates:
        raise ValueError("Không tìm thấy luồng video chính; ảnh bìa không được dùng làm video.")
    stream = min(candidates, key=lambda item: (
        not bool(_number(item.get("disposition", {}).get("default", 0))), int(item["index"])))
    width, height = int(stream["width"]), int(stream["height"])
    rotation = _number(stream.get("tags", {}).get("rotate", 0))
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = _number(side_data["rotation"])
    # FFmpeg/OpenCV autorotate. Use displayed, not encoded, geometry.
    if round(rotation) % 180 == 90:
        width, height = height, width
    fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))
    duration = _number(stream.get("duration")) or _number(data.get("format", {}).get("duration"))
    return MainVideo(int(stream["index"]), width, height, fps, duration, len(videos),
                     sum(item.get("codec_type") == "audio" for item in streams),
                     any(_number(item.get("disposition", {}).get("attached_pic", 0)) for item in videos))


def probe_main_video(path, timeout=10.0):
    proc = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               encoding="utf-8", errors="replace", timeout=timeout,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return select_main_video(json.loads(proc.stdout))


def validate_video_output(path, source, *, require_audio=False, expected_size=None):
    output = probe_main_video(path)
    expected_size = expected_size or (source.width, source.height)
    if output.video_stream_count != 1 or output.has_cover:
        raise ValueError("File kết quả phải có đúng một video chính, không có ảnh bìa.")
    if (output.width, output.height) != expected_size:
        raise ValueError(f"Sai kích thước video: {(output.width, output.height)} != {expected_size}")
    if require_audio and output.audio_stream_count != 1:
        raise ValueError("Video kết quả thiếu hoặc thừa luồng âm thanh lồng tiếng.")
    if source.fps > 0 and (output.fps <= 0 or abs(output.fps - source.fps) > max(0.2, source.fps * 0.01)):
        raise ValueError(f"Sai nhịp hình video: {output.fps:.6f}fps != {source.fps:.6f}fps")
    if source.duration > 0:
        tolerance = max(0.25, 2.0 / source.fps if source.fps > 0 else 0.25)
        if output.duration <= 0 or abs(output.duration - source.duration) > tolerance:
            raise ValueError(f"Sai thời lượng video: {output.duration:.3f}s != {source.duration:.3f}s")
    return output
