# -*- coding: utf-8 -*-
"""Scene Motion & Video Composer Service for Tool V2.

Adapted and enhanced from Akai1Shuichi/Editor-AI-App.
Features:
1. Cinematic camera motion engine using FFmpeg perspective filter (smooth sub-pixel interpolation, not jerky zoompan).
   Supports: zoom_in, zoom_out, pan_left, pan_right, pan_up, pan_down, drift_left, drift_right, shake.
2. Timeline synchronization between scene images, subtitle SRT cues, and speech audio.
3. Multi-aspect ratio video rendering: 9:16 (TikTok/Shorts/Reels), 16:9 (YouTube/Facebook), 1:1 (Square).
4. Auto-distribution: Automatically maps scenes to subtitle segments or evenly distributes across audio duration.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

DEFAULT_MOTION = {"type": "none", "strength": "subtle"}
MOTION_TYPES = [
    "auto",
    "zoom_in",
    "zoom_out",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
    "drift_left",
    "drift_right",
    "shake",
    "none",
]

MOTION_LABELS = {
    "auto": "✨ Tự động luân phiên (Đa dạng góc máy)",
    "zoom_in": "🔍 Phóng to chậm rãi (Zoom In - Kịch tính)",
    "zoom_out": "🔭 Thu nhỏ mở rộng (Zoom Out - Toàn cảnh)",
    "pan_left": "⬅️ Lia máy sang trái (Pan Left)",
    "pan_right": "➡️ Lia máy sang phải (Pan Right)",
    "pan_up": "⬆️ Lia máy lên trên (Pan Up)",
    "pan_down": "⬇️ Lia máy xuống dưới (Pan Down)",
    "drift_left": "🌊 Trôi góc máy trái (Drift Left - Nghệ thuật)",
    "drift_right": "🏄 Trôi góc máy phải (Drift Right - Nghệ thuật)",
    "shake": "⚡ Rung máy kịch tính (Camera Shake)",
    "none": "⏹️ Cảnh tĩnh (Static)",
}


def escape_ffconcat_path(path: str) -> str:
    """Escape a path for a single-quoted FFmpeg concat-demuxer entry."""
    return path.replace("'", r"'\''")


def get_ffmpeg_path() -> str:
    """Find ffmpeg binary path."""
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise RuntimeError("Không tìm thấy ffmpeg trong hệ thống.")


def parse_srt_time(time_str: str) -> float:
    """Convert SRT timestamp (HH:MM:SS,mmm) to seconds float."""
    time_str = time_str.strip()
    match = re.match(r"^(\d+):(\d+):(\d+)[,\.](\d+)$", time_str)
    if not match:
        raise ValueError(f"Định dạng thời gian không hợp lệ: '{time_str}'")
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / (10 ** len(millis))


def format_time(seconds: float) -> str:
    """Format seconds into MM:SS.mmm or HH:MM:SS.mmm."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:06.3f}"
    return f"{mins:02d}:{secs:06.3f}"


def parse_srt_file(srt_path: Union[str, Path]) -> Dict[int, Dict[str, Any]]:
    """Parse .srt subtitle file into dictionary of segments."""
    p = Path(srt_path)
    if not p.exists():
        return {}
    content = p.read_text(encoding="utf-8-sig", errors="ignore")
    blocks = re.split(r"\n\s*\n", content.strip())
    subtitles = {}

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        try:
            sub_id = int(lines[0])
            time_match = re.match(
                r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", lines[1]
            )
            if time_match:
                start_sec = parse_srt_time(time_match.group(1))
                end_sec = parse_srt_time(time_match.group(2))
                text = " ".join(lines[2:]) if len(lines) > 2 else ""
                subtitles[sub_id] = {
                    "id": sub_id,
                    "start": start_sec,
                    "end": end_sec,
                    "text": text,
                }
        except Exception:
            continue
    return subtitles


def get_audio_duration(audio_path: Union[str, Path]) -> float:
    """Measure total audio duration in seconds using ffprobe/ffmpeg."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            dur = float(data["format"]["duration"])
            if dur > 0:
                return dur
    except Exception:
        pass

    # Fallback via ffmpeg stderr
    cmd_fallback = ["ffmpeg", "-i", str(audio_path)]
    proc = subprocess.run(cmd_fallback, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors="ignore")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 10.0


def normalize_motion(value: Any) -> Dict[str, str]:
    """Validate and normalize motion configuration."""
    if not isinstance(value, dict):
        if isinstance(value, str) and value in MOTION_TYPES:
            return {"type": value, "strength": "subtle"}
        return DEFAULT_MOTION.copy()
    m_type = value.get("type", "none")
    if m_type not in MOTION_TYPES:
        m_type = "none"
    m_strength = value.get("strength", "subtle")
    if m_strength not in ("subtle", "medium"):
        m_strength = "subtle"
    return {"type": m_type, "strength": m_strength}


def _motion_range(motion: Dict[str, str]) -> Tuple[float, float, float, float, float, float]:
    kind, strength = motion["type"], motion["strength"]
    if kind == "zoom_in":
        return (1.0, 1.05 if strength == "subtle" else 1.10, 0.0, 0.0, 0.0, 0.0)
    if kind == "zoom_out":
        return (1.05 if strength == "subtle" else 1.10, 1.0, 0.0, 0.0, 0.0, 0.0)
    if kind.startswith("pan_"):
        distance = 0.03 if strength == "subtle" else 0.06
        scale = 1.06 if strength == "subtle" else 1.12
        x = distance * (1 if kind == "pan_right" else -1 if kind == "pan_left" else 0)
        y = distance * (1 if kind == "pan_down" else -1 if kind == "pan_up" else 0)
        return (scale, scale, 0.0, x, 0.0, y)
    if kind.startswith("drift_"):
        if strength == "subtle":
            start_scale, end_scale, start_x, end_x = 1.03, 1.06, 0.01, -0.02
        else:
            start_scale, end_scale, start_x, end_x = 1.04, 1.09, 0.02, -0.04
        direction = 1 if kind == "drift_left" else -1
        return (start_scale, end_scale, start_x * direction, end_x * direction, 0.0, 0.0)
    return (1.0, 1.0, 0.0, 0.0, 0.0, 0.0)


def _number(value: float) -> str:
    return f"{value:.8g}"


def _scene_expressions(
    motion: Dict[str, str], progress: str, frame: str, width: int, height: int, fps: int
) -> Tuple[str, str, str]:
    kind = motion["type"]
    if kind == "shake":
        amplitude = 2 if motion["strength"] == "subtle" else 4
        envelope = f"pow(sin(PI*({progress})),2)"
        scale = _number(1 + 2 * amplitude / min(width, height))
        x = f"({amplitude}/{width})*({envelope})*sin(2*PI*11*({frame})/{fps})"
        y = f"({amplitude}/{height})*({envelope})*cos(2*PI*13*({frame})/{fps})"
        return scale, x, y

    s0, s1, x0, x1, y0, y1 = _motion_range(motion)
    eased = f"(1-cos(PI*({progress})))/2"

    def between(start: float, end: float) -> str:
        if start == end:
            return _number(start)
        return f"({_number(start)}+({_number(end - start)})*({eased}))"

    return between(s0, s1), between(x0, x1), between(y0, y1)


def build_motion_filter(timeline: List[Dict[str, Any]], width: int, height: int, fps: int) -> str:
    """Build high-quality perspective zoom/pan FFmpeg expression for video scenes."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    expressions = []
    for item in timeline:
        start_frame = round(item["start"] * fps)
        end_frame = round(item["end"] * fps)
        frame = f"on-{start_frame + 1}"
        progress = f"min(1,max(0,({frame})/{max(1, end_frame - start_frame - 1)}))"
        expressions.append(
            (
                end_frame,
                _scene_expressions(
                    normalize_motion(item.get("motion")), progress, frame, width, height, fps
                ),
            )
        )

    def select(component: int) -> str:
        value = expressions[-1][1][component]
        for end_frame, parts in reversed(expressions[:-1]):
            value = f"if(lt(on,{end_frame + 1}),{parts[component]},{value})"
        return value

    zoom = select(0)
    tx = select(1)
    ty = select(2)
    x = f"max(0,min(W-W/({zoom}),(W-W/({zoom}))/2-({tx})*W/({zoom})))"
    y = f"max(0,min(H-H/({zoom}),(H-H/({zoom}))/2-({ty})*H/({zoom})))"
    right = f"({x}+W/({zoom}))"
    bottom = f"({y}+H/({zoom}))"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},"
        f"perspective=x0='{x}':y0='{y}':x1='{right}':y1='{y}':"
        f"x2='{x}':y2='{bottom}':x3='{right}':y3='{bottom}':"
        "sense=source:eval=frame:interpolation=cubic,"
        "format=yuv420p"
    )


def compute_composer_timeline(
    image_paths: List[Union[str, Path]],
    total_audio_duration: float,
    subtitles: Optional[Dict[int, Dict[str, Any]]] = None,
    default_motion: str = "auto",
) -> List[Dict[str, Any]]:
    """Generate aligned timeline with assigned camera motions for each scene."""
    valid_images = [Path(p).resolve() for p in image_paths if Path(p).is_file()]
    if not valid_images:
        raise ValueError("Không tìm thấy hình ảnh nào để dựng video!")

    num_scenes = len(valid_images)
    timeline: List[Dict[str, Any]] = []

    active_motions = [
        "zoom_in", "pan_left", "zoom_out", "pan_right",
        "drift_left", "pan_up", "drift_right"
    ]

    # Case A: If subtitles exist and match scene count or cues
    if subtitles and len(subtitles) >= num_scenes:
        sub_list = sorted(subtitles.values(), key=lambda s: s["start"])
        # Split subtitles across scenes
        chunk_size = max(1, len(sub_list) // num_scenes)
        for i in range(num_scenes):
            sub_idx = min(i * chunk_size, len(sub_list) - 1)
            start_t = sub_list[sub_idx]["start"] if i > 0 else 0.0

            m_kind = default_motion
            if m_kind == "auto":
                m_kind = active_motions[i % len(active_motions)]

            timeline.append({
                "index": i + 1,
                "scene_index": i + 1,
                "id": f"SC{i+1:02d}",
                "image": str(valid_images[i]),
                "image_path": str(valid_images[i]),
                "start": round(start_t, 3),
                "start_time": round(start_t, 3),
                "end": 0.0,
                "end_time": 0.0,
                "duration": 0.0,
                "motion": {"type": m_kind, "strength": "subtle"},
                "motion_type": m_kind,
            })

        for i in range(num_scenes):
            if i < num_scenes - 1:
                timeline[i]["end"] = timeline[i + 1]["start"]
            else:
                timeline[i]["end"] = round(max(total_audio_duration, timeline[i]["start"] + 1.0), 3)

            dur = round(timeline[i]["end"] - timeline[i]["start"], 3)
            if dur <= 0:
                dur = 1.0
                timeline[i]["end"] = timeline[i]["start"] + dur
            timeline[i]["duration"] = dur
            timeline[i]["end_time"] = timeline[i]["end"]

    else:
        # Case B: Even distribution of images across audio duration
        scene_dur = round(total_audio_duration / num_scenes, 3)
        cur_t = 0.0
        for i in range(num_scenes):
            end_t = round(total_audio_duration if i == num_scenes - 1 else cur_t + scene_dur, 3)
            m_kind = default_motion
            if m_kind == "auto":
                m_kind = active_motions[i % len(active_motions)]

            timeline.append({
                "index": i + 1,
                "scene_index": i + 1,
                "id": f"SC{i+1:02d}",
                "image": str(valid_images[i]),
                "image_path": str(valid_images[i]),
                "start": round(cur_t, 3),
                "start_time": round(cur_t, 3),
                "end": end_t,
                "end_time": end_t,
                "duration": round(end_t - cur_t, 3),
                "motion": {"type": m_kind, "strength": "subtle"},
                "motion_type": m_kind,
            })
            cur_t = end_t

    return timeline


def render_scene_composer_video(
    timeline: List[Dict[str, Any]],
    audio_path: Union[str, Path],
    output_path: Union[str, Path],
    total_audio_duration: float,
    aspect_ratio: str = "9:16",
    fps: int = 30,
    progress_callback: Optional[ProgressCallback] = None,
    is_cancelled: Optional[CancelCallback] = None,
) -> Dict[str, Any]:
    """Render complete video from image timeline and audio using FFmpeg."""
    ffmpeg_exe = get_ffmpeg_path()
    audio = Path(audio_path).resolve()
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if aspect_ratio == "16:9":
        width, height = 1920, 1080
    elif aspect_ratio == "1:1":
        width, height = 1080, 1080
    else:
        # 9:16 vertical standard for TikTok/Shorts/Reels
        width, height = 1080, 1920

    temp_concat = output.parent / f"temp_concat_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.txt"
    temp_filter = temp_concat.with_suffix(".filter")
    concat_lines = ["ffconcat version 1.0"]

    valid_items = [item for item in timeline if item.get("image") and Path(item["image"]).is_file()]
    if not valid_items:
        raise ValueError("Không tìm thấy file ảnh hợp lệ để dựng video!")

    for item in timeline:
        img_p = escape_ffconcat_path(Path(item["image"]).resolve().as_posix())
        dur = item["duration"]
        concat_lines.append(f"file '{img_p}'")
        concat_lines.append(f"duration {dur}")

    # Concat demuxer needs last file entry repeated
    last_img_p = escape_ffconcat_path(Path(valid_items[-1]["image"]).resolve().as_posix())
    concat_lines.append(f"file '{last_img_p}'")

    temp_concat.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    vf_arg = build_motion_filter(timeline, width, height, fps)
    temp_filter.write_text(vf_arg, encoding="utf-8")

    def cleanup():
        for f in (temp_concat, temp_filter):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass

    cmd = [
        ffmpeg_exe,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(temp_concat),
        "-i",
        str(audio),
        "-t",
        f"{total_audio_duration:.3f}",
        "-/filter:v",
        str(temp_filter),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-r",
        str(fps),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]

    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    if progress_callback:
        progress_callback(10, "Bắt đầu mã hóa khung hình và chuyển động camera...")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=flags,
        )

        # Parse FFmpeg stderr progress
        time_pattern = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
        while proc.poll() is None:
            if is_cancelled and is_cancelled():
                proc.terminate()
                cleanup()
                return {"success": False, "cancelled": True, "error": "Đã hủy bởi người dùng"}

            line = proc.stderr.readline()
            if not line:
                continue
            match = time_pattern.search(line)
            if match and total_audio_duration > 0:
                h, m, s = match.groups()
                curr_sec = int(h) * 3600 + int(m) * 60 + float(s)
                pct = int(min(98, max(10, (curr_sec / total_audio_duration) * 100)))
                if progress_callback:
                    progress_callback(pct, f"Đang dựng video: {format_time(curr_sec)} / {format_time(total_audio_duration)}")

        proc.wait()
        cleanup()

        if proc.returncode != 0:
            err_msg = proc.stderr.read()
            raise RuntimeError(f"FFmpeg render lỗi ({proc.returncode}): {err_msg}")

        if not output.is_file() or output.stat().st_size < 1000:
            raise RuntimeError("File video đầu ra bị lỗi hoặc rỗng.")

        if progress_callback:
            progress_callback(100, "Hoàn tất dựng video phân cảnh!")

        return {
            "success": True,
            "output_path": str(output),
            "duration": total_audio_duration,
            "size_mb": round(output.stat().st_size / (1024 * 1024), 2),
            "aspect_ratio": aspect_ratio,
            "total_scenes": len(timeline),
        }
    except Exception as e:
        cleanup()
        logger.error(f"Lỗi render scene video: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
