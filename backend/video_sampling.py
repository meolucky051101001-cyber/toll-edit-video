"""Bounded GPU sampling for analysis; never resizes the delivered source video."""
import json
import os
import subprocess
import tempfile
from pathlib import Path


def sample_video_frames(video_path, count=5, start=None, end=None, max_width=960):
    """Decode once on CUDA and transfer only selected, downscaled frames.

    Selection uses presentation timestamps, including on variable-FPS sources.
    GPU failure is explicit: no expensive silent CPU decoder fallback.
    """
    import cv2

    count = max(1, min(12, int(count)))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    options = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                   creationflags=flags)
    if end is None:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "json", str(video_path)], timeout=15, check=True, **options)
        end = float(json.loads(probe.stdout)["format"]["duration"])
    start, end = max(0.0, float(start or 0)), float(end)
    if end <= start:
        return []
    times = [start + (end - start) * i / (count + 1) for i in range(1, count + 1)]
    select = "+".join(
        "gte(t\\,{0:.9f})*(isnan(prev_selected_t)+lt(prev_selected_t\\,{0:.9f}))".format(t)
        for t in times)
    with tempfile.TemporaryDirectory(prefix="v2-visual-samples-") as directory:
        pattern = str(Path(directory) / "%03d.png")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(video_path),
            "-an", "-vf", "setpts=PTS-STARTPTS,select={},scale_cuda=w='min({},iw)':h=-2:format=nv12,hwdownload,format=nv12".format(select, int(max_width)),
            "-fps_mode", "vfr", "-frames:v", str(count), "-threads", "2", pattern],
            timeout=60, check=True, **options)
        frames = [cv2.imread(str(path)) for path in sorted(Path(directory).glob("*.png"))]
        if not frames or any(frame is None for frame in frames):
            raise RuntimeError("GPU video sampling returned missing/unreadable frames")
        return frames
