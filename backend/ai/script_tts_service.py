# -*- coding: utf-8 -*-
"""Service to generate CapCut TTS audio, align subtitles, and render complete scripts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import math
import subprocess
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests
from pydub import AudioSegment

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

logger = logging.getLogger(__name__)

TTS_DURATION_TOLERANCE = 0.3
TTS_MAX_REWRITE_ATTEMPTS = 2

def clean_fs_path(p: str | Path) -> Path:
    """Chuyển đổi đường dẫn từ Electron (local:///) hoặc chuỗi sang Path native của hệ điều hành."""
    if isinstance(p, Path):
        return p
    s = str(p or "").strip()
    if s.startswith("local:///"):
        s = s[len("local:///"):]
    import urllib.parse
    s = urllib.parse.unquote(s)
    return Path(s)


def resolve_capcut_api_dir() -> Optional[Path]:
    """
    Tìm thư mục chứa CapCut SDK (capcut-tts-api) theo thứ tự ưu tiên:
    1. Biến môi trường CAPCUT_API_DIR
    2. Thư mục bundled trong dự án (PROJECT_ROOT / 'capcut-tts-api')
    3. Thư mục cha (WORKSPACE_PARENT / 'capcut-tts-api')
    4. Thư mục hiện tại hoặc sys.path
    """
    candidates = []
    env_path = os.getenv("CAPCUT_API_DIR")
    if env_path:
        candidates.append(Path(env_path))

    this_file = Path(__file__).resolve()
    # parents[2] = project root (video-dubbing-app)
    candidates.append(this_file.parents[2] / "capcut-tts-api")
    # parents[3] = scratch directory
    if len(this_file.parents) > 3:
        candidates.append(this_file.parents[3] / "capcut-tts-api")

    for p in candidates:
        if p.exists() and (p / "capcut_tts_api").exists():
            return p.resolve()

    for p in candidates:
        if p.exists():
            return p.resolve()

    return None


# Thêm đường dẫn capcut-tts-api vào sys.path nếu cần
_CAPCUT_API_DIR = resolve_capcut_api_dir()
if _CAPCUT_API_DIR and str(_CAPCUT_API_DIR) not in sys.path:
    sys.path.insert(0, str(_CAPCUT_API_DIR))

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


def get_voice_metadata(voice_id: str) -> Optional[Dict[str, Any]]:
    """Tìm metadata của giọng đọc từ CAPCUT_VOICES."""
    for v in CAPCUT_VOICES:
        if v.get("id") == voice_id:
            return v
    return None


def get_edge_fallback_voice(voice_id: str) -> str:
    """Xác định giọng Edge TTS dự phòng chuẩn xác theo giới tính của giọng CapCut."""
    meta = get_voice_metadata(voice_id)
    if meta:
        gender = meta.get("gender", "").lower()
        if gender == "male":
            return "vi-VN-NamMinhNeural"
        elif gender == "female":
            return "vi-VN-HoaiMyNeural"
    
    # Fallback cho voice không nằm trong metadata
    logger.warning(f"Không tìm thấy metadata cho voice '{voice_id}', mặc định dùng Hoài My (Nữ).")
    return "vi-VN-HoaiMyNeural"


def format_srt_time(seconds: float) -> str:
    """Chuyển đổi giây sang định dạng SRT 00:00:00,000 sử dụng integer milliseconds chuẩn xác."""
    total_ms = max(0, int(round(seconds * 1000)))
    hrs = total_ms // 3600000
    mins = (total_ms % 3600000) // 60000
    secs = (total_ms % 60000) // 1000
    millis = total_ms % 1000
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
    fallback_voice = get_edge_fallback_voice(voice)
    logger.info(f"Sử dụng Edge TTS fallback voice: {fallback_voice} cho giọng gốc {voice}")
    async def _run_fallback():
        comm = edge_tts.Communicate(text, fallback_voice)
        await comm.save(str(output_path))
    asyncio.run(_run_fallback())
    audio = AudioSegment.from_file(str(output_path))
    return audio.duration_seconds


def split_scene_into_subtitle_chunks(
    text: str,
    start_time: float,
    duration: float,
    max_chars: int = 35
) -> List[Dict[str, Any]]:
    """Tách câu thoại phân cảnh thành các cụm phụ đề ngắn gọn chuẩn TikTok/Reels (< 35 ký tự)."""
    words = text.strip().split()
    if not words:
        return []
    chunks = []
    curr_chunk = []
    for w in words:
        candidate = " ".join(curr_chunk + [w])
        if len(candidate) <= max_chars or not curr_chunk:
            curr_chunk.append(w)
        else:
            chunks.append(" ".join(curr_chunk))
            curr_chunk = [w]
    if curr_chunk:
        chunks.append(" ".join(curr_chunk))

    total_words = len(words)
    result = []
    curr_t = start_time
    for chunk in chunks:
        c_words = len(chunk.split())
        c_dur = duration * (c_words / total_words) if total_words > 0 else duration
        c_end = curr_t + c_dur
        result.append({
            "text": chunk,
            "start": round(curr_t, 3),
            "end": round(c_end, 3)
        })
        curr_t = c_end
    return result


def fit_audio_to_scene_window(
    audio_path: str | Path,
    target_duration: float,
    available_window: float,
    output_path: Optional[str | Path] = None,
    max_speedup_ratio: float = 1.08,
    fade_out_ms: int = 80
) -> Tuple[Path, float, str]:
    """
    Đảm bảo audio phân cảnh không bao giờ tràn qua cửa sổ khả dụng (available_window)
    của phân cảnh tiếp theo, triệt tiêu hoàn toàn hiện tượng lồng tiếng chồng chéo (audio overlap).

    Quy tắc an toàn:
    1. duration <= available_window + 0.15s:
       Giữ nguyên audio gốc (status: 'pass' hoặc 'best_effort').
    2. available_window < duration <= available_window * max_speedup_ratio (1.08x):
       Áp dụng time-stretch (atempo) nhẹ bằng ffmpeg để khớp chính xác window mà không làm biến dạng giọng đọc (status: 'fitted').
    3. duration > available_window * max_speedup_ratio:
       Cắt an toàn (trim) tại available_window kèm fade-out 80ms ở cuối để tránh tiếng pop/click (status: 'trimmed').

    Trả về: (fitted_file_path, fitted_duration, status_label)
    """
    src_path = Path(audio_path)
    if not src_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file audio: {src_path}")

    dst_path = Path(output_path) if output_path else src_path.parent / f"{src_path.stem}_fitted{src_path.suffix}"
    audio = AudioSegment.from_file(str(src_path))
    curr_dur = audio.duration_seconds

    # Trường hợp 1: Audio nằm trọn vẹn trong window khả dụng
    if curr_dur <= available_window:
        if dst_path.resolve() != src_path.resolve():
            shutil.copy2(src_path, dst_path)
        status = "pass" if abs(curr_dur - target_duration) <= TTS_DURATION_TOLERANCE else "best_effort"
        return dst_path, round(curr_dur, 2), status

    # Trường hợp 2: Dài hơn window nhưng <= 1.08x window -> Áp dụng time-stretch (atempo)
    if curr_dur <= available_window * max_speedup_ratio:
        speed_factor = round(curr_dur / available_window, 4)
        speed_factor = max(1.0, min(1.08, speed_factor))
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", str(src_path),
            "-filter:a", f"atempo={speed_factor:.4f}",
            "-vn", str(dst_path)
        ]
        stretched = False
        try:
            res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and dst_path.exists():
                fitted_audio = AudioSegment.from_file(str(dst_path))
                dur_res = fitted_audio.duration_seconds
                if dur_res <= available_window + 0.02:
                    return dst_path, round(min(dur_res, available_window), 2), "fitted"
                else:
                    audio = fitted_audio
                    stretched = True
        except Exception as e:
            logger.warning(f"Lỗi time-stretch qua ffmpeg: {e}")

        # Fallback trim with fade-out nếu atempo không khả dụng hoặc vẫn dài
        target_ms = int(round(available_window * 1000))
        trimmed = audio[:target_ms]
        if len(trimmed) > fade_out_ms:
            trimmed = trimmed.fade_out(fade_out_ms)
        trimmed.export(str(dst_path), format=dst_path.suffix.lstrip(".") or "mp3")
        return dst_path, round(available_window, 2), "fitted"

    # Trường hợp 3: Vượt quá 1.08x window -> Trim an toàn với 80ms fade-out
    target_ms = int(round(available_window * 1000))
    trimmed = audio[:target_ms]
    if len(trimmed) > fade_out_ms:
        trimmed = trimmed.fade_out(fade_out_ms)
    trimmed.export(str(dst_path), format=dst_path.suffix.lstrip(".") or "mp3")
    final_dur = round(len(trimmed) / 1000.0, 2)
    return dst_path, final_dur, "trimmed"


def render_full_script_tts(
    scenes: List[Dict[str, Any]],
    voice: str = "BV562_streaming",
    workspace_dir: Optional[str | Path] = None,
    pause_between_scenes_ms: int = 350,
    video_duration: Optional[float] = None,
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sinh audio hoàn chỉnh cho toàn bộ kịch bản bằng CapCut TTS kết hợp Closed-Loop Rewrite:
    - Với từng phân cảnh: Sinh TTS, đo thời lượng thực tế (actual_duration).
    - So sánh với target_duration: Nếu lệch > 0.3s, tự động kích hoạt Closed-Loop Rewrite tối đa 2 lần.
    - Áp dụng Safe Fitting (fit_audio_to_scene_window): Tuyệt đối không cho phép âm thanh phân cảnh
      tràn sang phân cảnh tiếp theo, triệt tiêu hoàn toàn voice overlap.
    - Xây dựng audio track dạng Master Timeline: Đặt audio từng phân cảnh chuẩn xác tại planned_start.
      Loại bỏ hoàn toàn audio drift (không bị dồn trễ lũy kế khi scene trước dài hay ngắn).
    - Tạo file SRT chuẩn xác từng cụm phụ đề ngắn (< 35 ký tự) bám theo timeline giọng đọc thực tế.
    """
    job_id = f"script_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    if workspace_dir is None:
        workspace_dir = Path(__file__).resolve().parents[2] / "workspace" / job_id
    else:
        workspace_dir = Path(workspace_dir) / job_id
    workspace_dir.mkdir(parents=True, exist_ok=True)

    segments_dir = workspace_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    processed_scenes = []

    for i, scene in enumerate(scenes):
        scene_idx = scene.get("index", scene.get("scene_idx", i + 1))
        curr_text = str(scene.get("speaker_text") or scene.get("voiceover") or scene.get("text") or "").strip()
        segment_file = segments_dir / f"scene_{scene_idx:02d}.mp3"

        # Lấy mốc thời gian kế hoạch (planned timeline)
        planned_start = scene.get("planned_start")
        if planned_start is None:
            planned_start = scene.get("start_seconds", 0.0)
        planned_start = float(planned_start or 0.0)

        planned_end = scene.get("planned_end")
        if planned_end is None:
            planned_end = scene.get("end_seconds")
        if planned_end is not None:
            planned_end = float(planned_end)
        else:
            planned_end = planned_start + 3.0

        target_dur = scene.get("target_duration")
        if target_dur is None:
            target_dur = scene.get("duration_seconds")
        if target_dur is not None:
            target_dur = float(target_dur)
        else:
            target_dur = round(planned_end - planned_start, 2)

        prev_scene = scenes[i - 1] if i > 0 else None
        next_scene = scenes[i + 1] if i < len(scenes) - 1 else None

        # Pass 1: Sinh TTS lần đầu
        initial_dur = generate_single_tts(curr_text, segment_file, voice=voice)
        if i < len(scenes) - 1:
            time.sleep(0.3)

        initial_delta = round(initial_dur - target_dur, 2)
        candidates = [(0, abs(initial_delta), initial_dur, curr_text, segment_file)]
        attempts_made = 0

        # Closed-Loop Rewrite nếu vượt tolerance (+-0.3s)
        if abs(initial_delta) > TTS_DURATION_TOLERANCE:
            logger.info(
                f"[Closed-Loop TTS] Cảnh #{scene_idx} lệch thời lượng: actual={initial_dur:.2f}s, target={target_dur:.2f}s (delta={initial_delta:+.2f}s). "
                f"Đang kích hoạt AI viết lại..."
            )
            for attempt in range(1, TTS_MAX_REWRITE_ATTEMPTS + 1):
                attempts_made += 1
                try:
                    from ai.scriptwriting import rewrite_scene_for_duration
                    rewritten = rewrite_scene_for_duration(
                        scene={**scene, "speaker_text": curr_text},
                        target_duration=target_dur,
                        actual_duration=candidates[-1][2],
                        api_key=api_key,
                        previous_scene=prev_scene,
                        next_scene=next_scene,
                        profile=scene.get("profile")
                    )
                    new_text = str(rewritten.get("speaker_text", "")).strip()
                    if new_text and new_text != curr_text:
                        retry_file = segments_dir / f"scene_{scene_idx:02d}_retry{attempt}.mp3"
                        dur_retry = generate_single_tts(new_text, retry_file, voice=voice)
                        delta_retry = round(dur_retry - target_dur, 2)
                        logger.info(
                            f"[Closed-Loop TTS] Cảnh #{scene_idx} retry {attempt}: duration={dur_retry:.2f}s (delta={delta_retry:+.2f}s)"
                        )
                        candidates.append((attempt, abs(delta_retry), dur_retry, new_text, retry_file))
                        if abs(delta_retry) <= TTS_DURATION_TOLERANCE:
                            break
                        curr_text = new_text
                    else:
                        break
                except Exception as rw_err:
                    logger.warning(f"Lỗi closed-loop rewrite scene #{scene_idx}: {rw_err}")
                    break

        # Chọn ứng viên có abs(delta) nhỏ nhất
        best_candidate = min(candidates, key=lambda c: c[1])
        selected_attempt, best_abs_delta, best_dur, best_text, best_file = best_candidate

        # Xác định available_window an toàn cho cảnh này để chống tràn sang cảnh sau
        if next_scene is not None:
            ns_start = next_scene.get("planned_start")
            if ns_start is None:
                ns_start = next_scene.get("start_seconds")
            ns_start = float(ns_start) if ns_start is not None else planned_end
            available_window = max(0.5, ns_start - planned_start)
        else:
            if video_duration and float(video_duration) > planned_start:
                available_window = max(target_dur, float(video_duration) - planned_start)
            else:
                available_window = max(0.5, planned_end - planned_start)

        # Trạng thái ban đầu
        if selected_attempt == 0 and best_abs_delta <= TTS_DURATION_TOLERANCE:
            duration_status = "pass"
        elif selected_attempt > 0 and best_abs_delta <= TTS_DURATION_TOLERANCE:
            duration_status = "rewritten"
        else:
            duration_status = "best_effort"

        # Safe Fitting: đảm bảo audio không bao giờ tràn qua available_window
        fitted_file = segments_dir / f"scene_{scene_idx:02d}_fitted.mp3"
        final_file, final_dur, fit_status = fit_audio_to_scene_window(
            best_file,
            target_duration=target_dur,
            available_window=available_window,
            output_path=fitted_file
        )

        if fit_status in ("fitted", "trimmed"):
            duration_status = fit_status

        final_delta = round(final_dur - target_dur, 2)
        actual_end = round(planned_start + final_dur, 3)

        sc_copy = dict(scene)
        sc_copy["speaker_text"] = best_text
        sc_copy["word_count"] = len(best_text.split())
        sc_copy["planned_start"] = round(planned_start, 3)
        sc_copy["planned_end"] = round(planned_end, 3)
        sc_copy["target_duration"] = round(target_dur, 2)
        sc_copy["initial_duration"] = round(initial_dur, 2)
        sc_copy["initial_delta"] = initial_delta
        sc_copy["rewrite_attempts"] = attempts_made
        sc_copy["selected_attempt"] = selected_attempt
        sc_copy["actual_start"] = round(planned_start, 3)
        sc_copy["actual_end"] = actual_end
        sc_copy["actual_duration"] = round(final_dur, 2)
        sc_copy["final_delta"] = final_delta
        sc_copy["duration_delta"] = final_delta
        sc_copy["duration_status"] = duration_status

        # Aliases tương thích ngược
        sc_copy["start_seconds"] = round(planned_start, 3)
        sc_copy["end_seconds"] = actual_end
        sc_copy["duration_seconds"] = round(final_dur, 2)
        sc_copy["audio_file"] = str(final_file)
        sc_copy["audio_filename"] = f"{job_id}/segments/{final_file.name}"

        processed_scenes.append(sc_copy)

    # Xây dựng Master Timeline Audio Track (No Audio Drift)
    max_planned_end = max((s["planned_end"] for s in processed_scenes), default=0.0)
    max_actual_end = max((s["actual_end"] for s in processed_scenes), default=0.0)
    total_timeline_sec = max(float(video_duration or 0.0), max_planned_end, max_actual_end)

    total_timeline_ms = int(math.ceil(total_timeline_sec * 1000)) + 300
    master_audio = AudioSegment.silent(duration=total_timeline_ms)

    # Đặt audio từng phân cảnh tại planned_start chính xác (đã được safe fit)
    for sc in processed_scenes:
        sc_audio = AudioSegment.from_file(sc["audio_file"])
        pos_ms = max(0, int(round(sc["planned_start"] * 1000)))
        master_audio = master_audio.overlay(sc_audio, position=pos_ms)

    # Cắt gọn master audio đúng tổng thời lượng
    if total_timeline_sec > 0:
        master_audio = master_audio[:int(math.ceil(total_timeline_sec * 1000))]

    final_audio_path = workspace_dir / "full_voiceover.mp3"
    master_audio.export(str(final_audio_path), format="mp3")

    # Xây dựng phụ đề SRT dạng cụm ngắn (< 35 ký tự) chuẩn TikTok/Reels căn theo timeline thực tế
    srt_entries = []
    srt_idx = 1
    for i, sc in enumerate(processed_scenes):
        sc_text = sc["speaker_text"]
        sc_start = sc["planned_start"]
        sc_dur = sc["actual_duration"]
        sc_end = sc["actual_end"]
        if i < len(processed_scenes) - 1:
            next_start = processed_scenes[i + 1]["planned_start"]
            if sc_end > next_start:
                sc_end = next_start
                sc_dur = max(0.1, sc_end - sc_start)

        chunks = split_scene_into_subtitle_chunks(sc_text, sc_start, sc_dur, max_chars=35)
        sc["subtitle_chunks"] = chunks
        for ch in chunks:
            c_s = ch["start"]
            c_e = min(ch["end"], sc_end)
            if c_s >= c_e:
                continue
            srt_entry = f"{srt_idx}\n{format_srt_time(c_s)} --> {format_srt_time(c_e)}\n{ch['text']}\n"
            srt_entries.append(srt_entry)
            srt_idx += 1

    srt_content = "\n".join(srt_entries) + "\n"
    srt_file_path = workspace_dir / "subtitles.srt"
    with open(srt_file_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    return {
        "status": "success",
        "job_id": job_id,
        "total_duration": round(master_audio.duration_seconds, 2),
        "audio_path": str(final_audio_path),
        "audio_filename": f"{job_id}/full_voiceover.mp3",
        "srt_path": str(srt_file_path),
        "srt_filename": f"{job_id}/subtitles.srt",
        "srt_content": srt_content,
        "scenes": processed_scenes,
    }


def mix_voice_with_bgm(
    voice_audio_path: str | Path,
    bgm_audio_path: str | Path,
    output_path: str | Path,
    bgm_volume_db: float = -18.0,
    ducking_db: float = -12.0
) -> str:
    """Trộn track lồng tiếng với nhạc nền BGM tự động ducking (hạ âm lượng BGM khi có thoại)."""
    voice = AudioSegment.from_file(str(voice_audio_path))
    bgm = AudioSegment.from_file(str(bgm_audio_path))

    # Hạ âm lượng BGM cơ bản + ducking
    bgm_ducked = bgm + (bgm_volume_db + ducking_db)

    # Lặp BGM nếu ngắn hơn voice
    if len(bgm_ducked) < len(voice):
        loops = int(math.ceil(len(voice) / len(bgm_ducked)))
        bgm_ducked = bgm_ducked * loops
    bgm_ducked = bgm_ducked[:len(voice)]

    mixed = bgm_ducked.overlay(voice)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    mixed.export(str(out), format="mp3")
    return str(out)


def get_media_duration(file_path: str | Path) -> float:
    """Lấy thời lượng chính xác của video hoặc audio bằng ffprobe (fallback sang cv2)."""
    p = clean_fs_path(file_path)
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(p)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        dur = float(proc.stdout.strip())
        if dur > 0:
            return dur
    except Exception as probe_err:
        logger.warning(f"ffprobe không đo được duration cho {p}: {probe_err}")

    # Fallback qua OpenCV nếu là file video
    try:
        import cv2
        cap = cv2.VideoCapture(str(p))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and total > 0:
            return round(float(total / fps), 3)
    except Exception:
        pass

    return 0.0


def hex_to_rgba(hex_code: str, alpha: int = 235) -> tuple:
    """Chuyển mã màu HEX sang tuple RGBA."""
    hex_code = str(hex_code or "#A52A3A").lstrip("#")
    if len(hex_code) == 3:
        hex_code = "".join([c * 2 for c in hex_code])
    if len(hex_code) == 6:
        r = int(hex_code[0:2], 16)
        g = int(hex_code[2:4], 16)
        b = int(hex_code[4:6], 16)
        return (r, g, b, alpha)
    elif len(hex_code) == 8:
        r = int(hex_code[0:2], 16)
        g = int(hex_code[2:4], 16)
        b = int(hex_code[4:6], 16)
        a = int(hex_code[6:8], 16)
        return (r, g, b, a)
    return (175, 45, 75, alpha)


def create_news_hook_card(
    text: str,
    badge_text: str = "",
    show_badge: bool = False,
    card_width: int = 960,
    output_png: str = "news_card.png",
    bg_color: str | tuple = "#A52A3A",
    border_color: tuple = (255, 255, 255, 255),
    border_width: int = 4,
    corner_radius: int = 28,
    font_size: int = 42,
    badge_font_size: int = 28,
    alpha: int = 235
) -> str:
    """Tạo file PNG card text hook drama bo góc viền trắng tinh tế, màu nền tùy chỉnh."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow (PIL) chưa được cài đặt, bỏ qua tạo card PNG.")
        return ""

    if isinstance(bg_color, str):
        card_rgba = hex_to_rgba(bg_color, alpha=alpha)
    else:
        card_rgba = bg_color

    render_badge = show_badge and bool(badge_text and badge_text.strip())

    # Windows font candidates
    def _get_font(sz: int, bold: bool = True):
        for fp in [
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\seguiui.ttf",
            r"C:\Windows\Fonts\tahoma.ttf",
            r"C:\Windows\Fonts\calibrib.ttf" if bold else r"C:\Windows\Fonts\calibri.ttf",
        ]:
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, sz)
                except Exception:
                    continue
        return ImageFont.load_default()

    main_font = _get_font(font_size, bold=True)
    badge_font = _get_font(badge_font_size, bold=True) if render_badge else None

    # Text wrapping
    dummy_img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    dummy_draw = ImageDraw.Draw(dummy_img)

    words = text.split()
    lines = []
    current_line = []
    text_max_w = card_width - 80

    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = dummy_draw.textbbox((0, 0), test_line, font=main_font)
        line_w = bbox[2] - bbox[0]
        if line_w <= text_max_w:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    if current_line:
        lines.append(" ".join(current_line))

    line_spacing = int(font_size * 0.35)
    line_height = font_size + line_spacing
    total_text_h = len(lines) * line_height - line_spacing

    top_padding = 65 if render_badge else 45
    bottom_padding = 45
    card_height = top_padding + total_text_h + bottom_padding

    badge_offset_y = 20 if render_badge else 0
    canvas_w = card_width + 40
    canvas_h = card_height + badge_offset_y + 40

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    card_x0 = 20
    card_y0 = (badge_offset_y + 10) if render_badge else 20
    card_x1 = card_x0 + card_width
    card_y1 = card_y0 + card_height

    # Vẽ card nền màu tùy chọn + viền trắng
    draw.rounded_rectangle(
        [card_x0, card_y0, card_x1, card_y1],
        radius=corner_radius,
        fill=card_rgba,
        outline=border_color,
        width=border_width
    )

    # Vẽ badge nếu bật
    if render_badge:
        b_text = badge_text.strip().upper()
        badge_bbox = draw.textbbox((0, 0), b_text, font=badge_font)
        badge_w = (badge_bbox[2] - badge_bbox[0]) + 36
        badge_h = (badge_bbox[3] - badge_bbox[1]) + 20
        badge_x0 = card_x0 + 40
        badge_y0 = card_y0 - (badge_h // 2)
        badge_x1 = badge_x0 + badge_w
        badge_y1 = badge_y0 + badge_h

        draw.rounded_rectangle(
            [badge_x0, badge_y0, badge_x1, badge_y1],
            radius=badge_h // 2,
            fill=(220, 35, 55, 255),
            outline=border_color,
            width=border_width
        )
        text_x = badge_x0 + (badge_w - (badge_bbox[2] - badge_bbox[0])) // 2
        text_y = badge_y0 + (badge_h - (badge_bbox[3] - badge_bbox[1])) // 2 - 2
        draw.text((text_x, text_y), b_text, font=badge_font, fill=(255, 255, 255, 255))

    # Vẽ các dòng text chính
    cur_y = card_y0 + top_padding
    for line in lines:
        l_bbox = draw.textbbox((0, 0), line, font=main_font)
        l_w = l_bbox[2] - l_bbox[0]
        l_x = card_x0 + (card_width - l_w) // 2
        draw.text((l_x, cur_y), line, font=main_font, fill=(255, 255, 255, 255))
        cur_y += line_height

    img.save(output_png)
    return output_png


def burn_script_to_video(
    video_path: str | Path,
    audio_path: str | Path,
    srt_path: str | Path,
    output_path: str | Path,
    hook_card_text: Optional[str] = None,
    hook_card_bg_color: str = "#A52A3A",
    hook_card_show_badge: bool = False,
    hook_card_badge_text: str = "",
    hook_card_duration: float = 4.5
) -> str:
    """
    Lồng tiếng CapCut, dập phụ đề SRT và (tùy chọn) dập Text Hook Drama Card vào video nền bằng FFmpeg.
    - hook_card_text: Chuỗi text hook hiển thị ở 4-5 giây đầu (nếu có).
    - hook_card_bg_color: Mã màu HEX cho nền thẻ (mặc định #A52A3A).
    - hook_card_show_badge: Bật/tắt huy hiệu (mặc định False: không có chữ NEWS).
    """
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    clean_video = str(clean_fs_path(video_path))
    clean_audio = str(clean_fs_path(audio_path))

    # 1. Đo thời lượng video gốc
    source_duration = get_media_duration(clean_video)
    logger.info(f"Thời lượng video gốc: {source_duration:.2f}s")

    # Chuẩn hóa đường dẫn srt cho FFmpeg Windows (thay \ bằng / và escape dấu :)
    clean_srt = str(Path(srt_path).resolve()).replace("\\", "/")
    if ":" in clean_srt:
        drive, rest = clean_srt.split(":", 1)
        clean_srt = f"{drive}\\:{rest}"

    # Font chữ to, viền đen nổi bật chuẩn TikTok/Reels
    subtitle_filter = (
        f"subtitles='{clean_srt}':force_style='FontSize=18,PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=35'"
    )

    # 2. Xử lý hook card nếu có
    temp_card_path = None
    if hook_card_text and hook_card_text.strip():
        temp_card_path = out_file.parent / f"temp_hook_{out_file.stem}_{uuid.uuid4().hex[:6]}.png"
        create_news_hook_card(
            text=hook_card_text.strip(),
            badge_text=hook_card_badge_text,
            show_badge=hook_card_show_badge,
            output_png=str(temp_card_path),
            bg_color=hook_card_bg_color
        )

    # 3. Xử lý filter complex
    audio_clause = f"[1:a]apad=whole_dur={source_duration},atrim=0:{source_duration}[a]" if source_duration > 0 else ""
    duration_args = ["-t", str(round(source_duration, 3))] if source_duration > 0 else []

    extra_inputs = []
    if temp_card_path and temp_card_path.exists():
        extra_inputs = ["-i", str(temp_card_path)]
        overlay_dur = min(hook_card_duration or 4.5, source_duration if source_duration > 0 else 999.0)
        video_clause = (
            f"[0:v]{subtitle_filter}[subbed];"
            f"[subbed][2:v]overlay=x=(W-w)/2:y=(H*0.52)-(h/2):enable='between(t,0,{overlay_dur})'[v]"
        )
    else:
        video_clause = f"[0:v]{subtitle_filter}[v]"

    if audio_clause:
        filter_complex = f"{video_clause};{audio_clause}"
    else:
        filter_complex = video_clause

    cmd = [
        "ffmpeg", "-y",
        "-i", clean_video,
        "-i", clean_audio,
    ] + extra_inputs + [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]" if source_duration > 0 else "1:a",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
    ] + duration_args + [str(out_file)]

    logger.info(f"Đang chạy FFmpeg: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            logger.error(f"FFmpeg error: {proc.stderr}")
            raise RuntimeError(f"Lỗi khi render video: {proc.stderr[-300:]}")
    finally:
        if temp_card_path and temp_card_path.exists():
            try:
                temp_card_path.unlink()
            except Exception:
                pass

    return str(out_file)
