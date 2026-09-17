# -*- coding: utf-8 -*-
"""Service to generate CapCut TTS audio, align subtitles, and render complete scripts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
from pydub import AudioSegment

logger = logging.getLogger(__name__)

# Thêm đường dẫn capcut-tts-api vào sys.path nếu cần
_candidate_capcut_paths = [
    Path(os.getenv("CAPCUT_API_DIR", "")),
    Path(r"C:\Users\admin\.gemini\antigravity\scratch\capcut-tts-api"),
    Path(__file__).resolve().parents[3] / "capcut-tts-api",
    Path(__file__).resolve().parents[2] / "capcut-tts-api",
]
for _p in _candidate_capcut_paths:
    if _p and _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
        break

try:
    from capcut_tts_api import CapCutClient
except Exception as e:
    logger.warning(f"Không thể nạp CapCutClient: {e}")
    CapCutClient = None

# Singleton CapCut client để tránh tạo mới mỗi lần gọi
_capcut_client_instance = None

def _get_capcut_client():
    global _capcut_client_instance
    if _capcut_client_instance is None and CapCutClient is not None:
        _capcut_client_instance = CapCutClient()
    return _capcut_client_instance

# Danh mục giọng đọc CapCut Việt Nam tối ưu cho mạng xã hội
CAPCUT_VOICES = [
    {
        "id": "BV562_streaming",
        "name": "Mai (Nữ Chuẩn)",
        "desc": "Giọng nữ chuẩn, truyền cảm, tự nhiên và phổ biến nhất trên TikTok / CapCut",
        "gender": "female",
        "provider": "capcut"
    },
    {
        "id": "BV075_streaming",
        "name": "Thanh Niên Tự Tin (Nam Trẻ)",
        "desc": "Giọng nam trẻ trung, dứt khoát, phong thái tự tin lôi cuốn",
        "gender": "male",
        "provider": "capcut"
    },
    {
        "id": "multi_male_felipe_uranus_bigtts",
        "name": "Nam Trầm (Chuyên Gia)",
        "desc": "Giọng nam trầm ấm, nghiêm túc, tạo cảm giác chuyên nghiệp và uy tín",
        "gender": "male",
        "provider": "capcut"
    },
    {
        "id": "BV421_vivn_streaming",
        "name": "Nhỏ Ngọt Ngào (Nữ Dễ Thương)",
        "desc": "Giọng nữ nhẹ nhàng, đáng yêu, phù hợp vlog đời sống & review ẩm thực",
        "gender": "female",
        "provider": "capcut"
    },
    {
        "id": "BV074_streaming",
        "name": "Cô Gái Hoạt Ngôn (Nữ Năng Động)",
        "desc": "Giọng nữ nói nhanh, hoạt bát, tràn đầy năng lượng",
        "gender": "female",
        "provider": "capcut"
    },
    {
        "id": "multi_female_richgirl_uranus_bigtts",
        "name": "Review Phim (Kịch Tính)",
        "desc": "Giọng kịch tính, lôi cuốn, chuyên dụng cho tóm tắt phim & kể chuyện bí ẩn",
        "gender": "female",
        "provider": "capcut"
    },
    {
        "id": "vi-VN-HoaiMyNeural",
        "name": "Hoài My (Edge TTS)",
        "desc": "Giọng đọc trí tuệ nhân tạo Microsoft, rõ ràng, tốc độ đều đặn",
        "gender": "female",
        "provider": "edge"
    },
    {
        "id": "vi-VN-NamMinhNeural",
        "name": "Nam Minh (Edge TTS)",
        "desc": "Giọng nam Microsoft, phát âm chuẩn xác, rành mạch",
        "gender": "male",
        "provider": "edge"
    },
]


def format_srt_time(seconds: float) -> str:
    """Chuyển đổi giây sang định dạng SRT 00:00:00,000"""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis = 0
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def generate_single_tts(text: str, output_file: str | Path, voice: str = "BV562_streaming") -> float:
    """
    Sinh audio TTS cho một câu thoại bằng CapCut TTS (hoặc fallback sang Edge TTS).
    Trả về thời lượng audio thực tế (tính bằng giây).
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = text.strip()
    if not text:
        # Nếu câu rỗng, tạo file im lặng 0.5s
        silence = AudioSegment.silent(duration=500)
        silence.export(str(output_path), format="mp3")
        return 0.5

    # 1. Nếu là giọng Edge TTS
    if voice.startswith("vi-VN-"):
        import edge_tts
        async def _run_edge():
            comm = edge_tts.Communicate(text, voice)
            await comm.save(str(output_path))
        asyncio.run(_run_edge())
        audio = AudioSegment.from_file(str(output_path))
        return audio.duration_seconds

    # 2. Sinh bằng CapCut TTS
    if CapCutClient is not None:
        try:
            client = _get_capcut_client()
            if client is None:
                raise RuntimeError("CapCutClient chưa sẵn sàng")
            res = client.generate_speech(texts=text, voice=voice, wait=True)
            tasks = (res.get("data") or {}).get("tasks") or []
            if tasks:
                payload = json.loads(tasks[0].get("payload", "{}"))
                audio_subs = payload.get("audio_subtitles", [])
                if audio_subs and audio_subs[0].get("speech_url"):
                    speech_url = audio_subs[0]["speech_url"]
                    r = requests.get(speech_url, timeout=30)
                    r.raise_for_status()
                    with open(output_path, "wb") as f:
                        f.write(r.content)
                    audio = AudioSegment.from_file(str(output_path))
                    return audio.duration_seconds
        except Exception as capcut_err:
            logger.warning(f"CapCut TTS gặp lỗi: {capcut_err}, đang chuyển sang Edge TTS fallback...")

    # 3. Fallback sang Edge TTS nếu CapCut TTS gặp sự cố
    import edge_tts
    fallback_voice = "vi-VN-HoaiMyNeural" if "female" in voice.lower() or "mai" in voice.lower() else "vi-VN-NamMinhNeural"
    async def _run_fallback():
        comm = edge_tts.Communicate(text, fallback_voice)
        await comm.save(str(output_path))
    asyncio.run(_run_fallback())
    audio = AudioSegment.from_file(str(output_path))
    return audio.duration_seconds


def render_full_script_tts(
    scenes: List[Dict[str, Any]],
    voice: str = "BV562_streaming",
    workspace_dir: Optional[str | Path] = None,
    pause_between_scenes_ms: int = 350
) -> Dict[str, Any]:
    """
    Sinh audio hoàn chỉnh cho toàn bộ kịch bản bằng CapCut TTS:
    - Sinh file audio cho từng cảnh.
    - Đo thời lượng thực tế từng cảnh.
    - Tạo file SRT chuẩn xác từng miligiây.
    - Ghép thành track audio duy nhất có độ giãn nghỉ tự nhiên.
    """
    job_id = f"script_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    if workspace_dir is None:
        workspace_dir = Path(__file__).resolve().parents[2] / "workspace" / job_id
    else:
        workspace_dir = Path(workspace_dir) / job_id
    workspace_dir.mkdir(parents=True, exist_ok=True)

    segments_dir = workspace_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    full_audio = AudioSegment.empty()
    pause_audio = AudioSegment.silent(duration=pause_between_scenes_ms)

    srt_entries = []
    processed_scenes = []
    current_time_seconds = 0.0

    for i, scene in enumerate(scenes):
        scene_idx = scene.get("index", i + 1)
        text = str(scene.get("speaker_text", "")).strip()
        segment_file = segments_dir / f"scene_{scene_idx:02d}.mp3"

        # Sinh TTS
        duration = generate_single_tts(text, segment_file, voice=voice)
        
        # Throttle giữa các request để tránh bị CapCut rate-limit
        if i < len(scenes) - 1:
            time.sleep(0.5)

        scene_audio = AudioSegment.from_file(str(segment_file))
        if len(full_audio) > 0:
            full_audio += pause_audio
            current_time_seconds += (pause_between_scenes_ms / 1000.0)

        start_time = current_time_seconds
        end_time = start_time + duration

        # Tạo mục SRT
        srt_entry = f"{i + 1}\n{format_srt_time(start_time)} --> {format_srt_time(end_time)}\n{text}\n"
        srt_entries.append(srt_entry)

        full_audio += scene_audio
        current_time_seconds += duration

        scene_copy = dict(scene)
        scene_copy["start_seconds"] = round(start_time, 3)
        scene_copy["end_seconds"] = round(end_time, 3)
        scene_copy["actual_duration"] = round(duration, 2)
        scene_copy["audio_file"] = str(segment_file)
        scene_copy["audio_filename"] = f"{job_id}/segments/{segment_file.name}"
        processed_scenes.append(scene_copy)

    # Xuất file audio gộp
    final_audio_path = workspace_dir / "full_voiceover.mp3"
    full_audio.export(str(final_audio_path), format="mp3")

    # Xuất file SRT
    srt_content = "\n".join(srt_entries) + "\n"
    srt_file_path = workspace_dir / "subtitles.srt"
    with open(srt_file_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    return {
        "job_id": job_id,
        "total_duration": round(current_time_seconds, 2),
        "audio_path": str(final_audio_path),
        "audio_filename": f"{job_id}/full_voiceover.mp3",
        "srt_path": str(srt_file_path),
        "srt_filename": f"{job_id}/subtitles.srt",
        "srt_content": srt_content,
        "scenes": processed_scenes,
    }


def burn_script_to_video(
    video_path: str,
    audio_path: str,
    srt_path: str,
    output_path: str
) -> str:
    """
    Lồng tiếng CapCut và dập phụ đề SRT vào video nền bằng FFmpeg.
    """
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # Chuẩn hóa đường dẫn srt cho FFmpeg Windows (thay \ bằng / và escape dấu :)
    clean_srt = str(Path(srt_path).resolve()).replace("\\", "/")
    if ":" in clean_srt:
        drive, rest = clean_srt.split(":", 1)
        clean_srt = f"{drive}\\:{rest}"

    # Lệnh FFmpeg: Thay thế/trộn audio và gắn phụ đề
    # Font chữ to, viền đen nổi bật chuẩn TikTok/Reels
    subtitle_filter = (
        f"subtitles='{clean_srt}':force_style='FontSize=18,PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=35'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-filter_complex", f"[0:v]{subtitle_filter}[v]",
        "-map", "[v]",
        "-map", "1:a",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(out_file)
    ]

    logger.info(f"Đang chạy FFmpeg: {' '.join(cmd)}")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        logger.error(f"FFmpeg error: {proc.stderr}")
        raise RuntimeError(f"Lỗi khi render video: {proc.stderr[-300:]}")

    return str(out_file)
