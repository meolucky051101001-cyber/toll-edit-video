# -*- coding: utf-8 -*-
"""
Segment Editor Service (Phase C) for Tool V1 & Tool V2.
Allows viewing, editing, individual sentence regeneration, and safe publishing of revised videos.
Guarantees optimistic locking (revisions), preservation of original finished artifacts,
and intelligent atempo adjustment when audio exceeds the timeline window.
"""

from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import tempfile
from copy import deepcopy
from pipeline_v2.atomic_io import atomic_copy_file
from v1_integrity_errors import AudioDurationError, FrozenConfigError, SegmentCommitError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from backend.config.paths import AppPaths
except ImportError:
    from config.paths import AppPaths

PATHS = AppPaths.from_environment(Path(__file__).resolve().parents[1])

# Concurrency lock for regeneration and publishing
_segment_lock = asyncio.Lock()

# Cache for idempotency keys (TTL: 5 minutes)
_IDEMPOTENCY_CACHE: Dict[Tuple[str, str], Tuple[float, Tuple[int, Tuple[str, ...]], Dict[str, Any]]] = {}


def resolve_workspace_dir() -> Path:
    PATHS.workspace.mkdir(parents=True, exist_ok=True)
    return PATHS.workspace


def resolve_output_dir() -> Path:
    return PATHS.output_dir


def _workspace_roots() -> List[Path]:
    roots = [resolve_workspace_dir()]
    shared = PATHS.shared_workspace_dir
    if shared and shared not in roots:
        roots.append(shared)
    return roots


def get_segment_drafts_dir() -> Path:
    ws = resolve_workspace_dir()
    d = ws / "control" / "segment_drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parse_srt_time(s: str) -> float:
    """Chuyển đổi chuỗi thời gian SRT (00:01:23,456) thành số giây (float)."""
    s = s.replace(",", ".").strip()
    parts = s.split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def format_srt_time(sec: float) -> str:
    """Chuyển đổi số giây thành định dạng chuẩn SRT (00:01:23,456)."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(srt_text: str) -> List[Dict[str, Any]]:
    """Phân tích nội dung file SRT thành danh sách các segment có cấu trúc."""
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    segments = []
    time_pat = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"
    )

    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if not lines:
            continue
        # Check if line 0 or line 1 has time
        time_match = None
        idx = None
        text_lines = []

        if time_pat.search(lines[0]):
            time_match = time_pat.search(lines[0])
            text_lines = lines[1:]
        elif len(lines) > 1 and time_pat.search(lines[1]):
            try:
                idx = int(lines[0])
            except ValueError:
                idx = len(segments) + 1
            time_match = time_pat.search(lines[1])
            text_lines = lines[2:]

        if time_match:
            start_sec = parse_srt_time(time_match.group(1))
            end_sec = parse_srt_time(time_match.group(2))
            if idx is None:
                idx = len(segments) + 1
            content = " ".join(text_lines).strip()
            segments.append({
                "index": idx,
                "start": round(start_sec, 2),
                "end": round(end_sec, 2),
                "duration": round(end_sec - start_sec, 2),
                "content": content,
            })

    return segments


def format_srt(segments: List[Dict[str, Any]]) -> str:
    """Tạo chuỗi SRT từ danh sách segments."""
    out = []
    for s in segments:
        idx = s.get("index", 1)
        start_str = format_srt_time(s.get("start", 0.0))
        end_str = format_srt_time(s.get("end", 0.0))
        text = s.get("translated_text") or s.get("content") or ""
        out.append(f"{idx}\n{start_str} --> {end_str}\n{text}\n")
    return "\n".join(out)


class InvalidJobIdentifier(ValueError):
    """Định danh job chứa ký tự đường dẫn/ký tự cấm — từ chối để chặn path traversal."""


def sanitize_job_id(job_id_or_name: str) -> str:
    """Làm sạch định danh job/tên file để tìm kiếm an toàn (chặn path traversal)."""
    name = str(job_id_or_name).strip()
    if name.startswith("Dubbed_"):
        name = name[len("Dubbed_"):]
    if name.endswith(".mp4"):
        name = name[:-4]
    name = name.strip()
    if not name or name in (".", "..") or name != Path(name).name or any(ch in '<>:"|?*' for ch in name):
        raise InvalidJobIdentifier("Định danh job không hợp lệ (không cho phép đường dẫn hoặc ký tự cấm)")
    return name


def find_job_workspace(job_id_or_name: str) -> Optional[Path]:
    """Tìm thư mục workspace của job trên cả V1 và V2."""
    clean_id = sanitize_job_id(job_id_or_name)
    roots = _workspace_roots()

    for root in roots:
        if not root.is_dir():
            continue

        # 1. Exact match
        exact = root / clean_id
        if exact.is_dir():
            return exact

        # 2. Match with original filename or batch prefix
        for child in root.iterdir():
            if child.is_dir():
                if clean_id in child.name or child.name in clean_id:
                    return child

    return None


_AUDIO_DURATION_CACHE: Dict[str, tuple] = {}


def get_audio_duration_seconds(file_path: Path) -> float:
    """Measure with ffprobe/audio decoder; never infer duration from byte size."""
    cache_key = str(file_path)
    try:
        st = file_path.stat()
        cached = _AUDIO_DURATION_CACHE.get(cache_key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
    except Exception as _phase6_exc:
        logger.debug('Suppressed error in segment_editor_service.get_audio_duration_seconds (file_path.stat): %s', type(_phase6_exc).__name__, exc_info=False)

    duration = 0.0
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            duration = float(res.stdout.strip())
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid probed duration")
    except Exception as _phase6_exc:
        logger.warning('Suppressed error in segment_editor_service.get_audio_duration_seconds (str): %s', type(_phase6_exc).__name__, exc_info=False)
    if not math.isfinite(duration) or duration <= 0.0:
        try:
            import soundfile as sf
            duration = float(sf.info(str(file_path)).duration)
        except Exception as exc:
            raise AudioDurationError("Không đo được thời lượng audio bằng decoder; chưa thể sửa tốc độ câu.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise AudioDurationError("Thời lượng audio không hợp lệ; chưa thể sửa tốc độ câu.")
    duration = round(duration, 3)
    if duration <= 0:
        raise AudioDurationError("Audio quá ngắn để xác minh thời lượng.")

    try:
        st = file_path.stat()
        _AUDIO_DURATION_CACHE[cache_key] = (st.st_mtime_ns, st.st_size, duration)
    except Exception as _phase6_exc:
        logger.debug('Suppressed error in segment_editor_service.get_audio_duration_seconds (file_path.stat): %s', type(_phase6_exc).__name__, exc_info=False)
    return duration


def resolve_job_voice(job_id_or_name: str, job_folder: Optional[Path] = None) -> Tuple[str, str]:
    """
    Xác định chính xác giọng đọc thực tế đã được dùng để lồng tiếng cho video này.
    Trả về Tuple[voice_id, voice_label].
    """
    clean_id = sanitize_job_id(job_id_or_name)
    prefix_id = clean_id[:18] if len(clean_id) >= 18 else clean_id

    # 1. Tra cứu trực tiếp trong job_folder
    if job_folder and job_folder.is_dir():
        for vl in list(job_folder.rglob("voice_lock.json")) + list(job_folder.glob("voice_lock.json")):
            if vl.is_file():
                try:
                    data = json.loads(vl.read_text(encoding="utf-8"))
                    v_id = data.get("voice_id")
                    if v_id:
                        lbl = data.get("voice_label") or v_id
                        return v_id, lbl
                except Exception as _phase6_exc:
                    logger.debug('Suppressed error in segment_editor_service.resolve_job_voice (json.loads): %s', type(_phase6_exc).__name__, exc_info=False)

        for mf in list(job_folder.rglob("job_manifest.json")) + list(job_folder.glob("job_manifest.json")):
            if mf.is_file():
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    req = m.get("metadata", {}).get("request", {})
                    if req.get("voice_id"):
                        return req["voice_id"], req.get("voice_label", req["voice_id"])
                    param = str(req.get("voice_param", ""))
                    if "BV562" in param:
                        return "capcut-BV562_streaming", "CapCut · Ô Mai"
                    if "BV421" in param:
                        return "capcut-BV421_vivn_streaming", "CapCut · Nhã Ngọt Ngào"
                    if "HoaiMy" in param:
                        return "microsoft-hoaimy", "Microsoft · Hoài My (nữ)"
                    if "NamMinh" in param:
                        return "microsoft-namminh", "Microsoft · Nam Minh (nam)"
                except Exception as _phase6_exc:
                    logger.debug('Suppressed error in segment_editor_service.resolve_job_voice (json.loads): %s', type(_phase6_exc).__name__, exc_info=False)

    # 2. Tìm kiếm trong các thư mục workspace theo clean_id hoặc prefix
    roots = _workspace_roots()
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and (clean_id in child.name or prefix_id in child.name):
                for vl in child.rglob("voice_lock.json"):
                    if vl.is_file():
                        try:
                            data = json.loads(vl.read_text(encoding="utf-8"))
                            if data.get("voice_id"):
                                return data["voice_id"], data.get("voice_label") or data["voice_id"]
                        except Exception as _phase6_exc:
                            logger.debug('Suppressed error in segment_editor_service.resolve_job_voice (json.loads): %s', type(_phase6_exc).__name__, exc_info=False)
                for mf in child.rglob("job_manifest.json"):
                    if mf.is_file():
                        try:
                            m = json.loads(mf.read_text(encoding="utf-8"))
                            req = m.get("metadata", {}).get("request", {})
                            if req.get("voice_id"):
                                return req["voice_id"], req.get("voice_label", req["voice_id"])
                        except Exception as _phase6_exc:
                            logger.debug('Suppressed error in segment_editor_service.resolve_job_voice (json.loads): %s', type(_phase6_exc).__name__, exc_info=False)

    # 3. Tra cứu từ JobConfigService (Frozen snapshot hoặc Video Drafts)
    try:
        import job_config_service
        frozen = job_config_service.get_frozen_config(clean_id) or job_config_service.get_frozen_config(prefix_id)
        if frozen and frozen.get("effective_config", {}).get("voice_id"):
            v_id = frozen["effective_config"]["voice_id"]
            return v_id, frozen["effective_config"].get("voice_label", v_id)
        draft = job_config_service.get_video_draft(clean_id) or job_config_service.get_video_draft(prefix_id)
        if draft and draft.get("overrides", {}).get("voice_id"):
            v_id = draft["overrides"]["voice_id"]
            return v_id, draft["overrides"].get("voice_label", v_id)
    except FrozenConfigError:
        raise
    except Exception as _phase6_exc:
        logger.debug('Suppressed error in segment_editor_service.resolve_job_voice (job_config_service.get_frozen_config): %s', type(_phase6_exc).__name__, exc_info=False)

    # 4. Tra cứu từ voice_selection (giọng đang được chọn trên Dashboard)
    try:
        import voice_selection
        sel = voice_selection.selected()
        if sel and sel.get("id"):
            return sel["id"], sel.get("label", sel["id"])
    except Exception as _phase6_exc:
        logger.debug('Suppressed error in segment_editor_service.resolve_job_voice (voice_selection.selected): %s', type(_phase6_exc).__name__, exc_info=False)

    # 5. Fallback thông minh
    return "capcut-BV562_streaming", "CapCut · Mai (mặc định)"


def estimate_segment_speed(
    text: str,
    audio_dur: float,
    window_dur: float = 0.0,
    meta_speed: Optional[float] = None,
) -> float:
    """
    Xác định tốc độ nói thực tế (speed multiplier) của câu trong video:
    1. Nếu có tốc độ ghi nhận trực tiếp từ metadata (applied_atempo hoặc speed_ratio > 0), dùng trực tiếp.
    2. Nếu không, ước lượng từ số từ tiếng Việt (word count) so với thời lượng audio thực tế:
       - Tốc độ đàm thoại tự nhiên chuẩn 1.00x của TTS tiếng Việt (CapCut/Edge) là ~3.8-4.0 từ/giây (khoảng 0.25s/từ + 0.15s đệm ngắt).
       - Tỷ lệ tốc độ = expected_1x_dur / audio_dur.
       - Nếu audio_dur >= expected_1x_dur * 0.95 (câu ngắn trong khung dài), giữ 1.00x vì pipeline không ép chậm.
       - Làm tròn bước 0.05 và giới hạn an toàn trong khoảng [0.70x, 2.00x].
    """
    if meta_speed is not None and float(meta_speed) > 0.05:
        ms = float(meta_speed)
        return max(0.70, min(2.00, round(ms * 20) / 20.0))

    if not text or audio_dur <= 0.1:
        return 1.0

    words = len(text.strip().split())
    if words == 0:
        return 1.0

    expected_dur = 0.15 + (words * 0.25)
    if audio_dur >= expected_dur * 0.95:
        return 1.0

    ratio = expected_dur / audio_dur
    stepped = round(ratio * 20) / 20.0
    return max(0.70, min(2.00, stepped))


def get_job_segments(job_id_or_name: str) -> Dict[str, Any]:
    """
    Truy xuất toàn bộ danh sách segment, câu gốc, câu dịch, audio từng câu và revision.
    Đọc từ bản nháp (nếu có), hoặc từ artifacts của pipeline (V1 hoặc V2).
    """
    clean_id = sanitize_job_id(job_id_or_name)
    draft_path = get_segment_drafts_dir() / f"{clean_id}.json"
    job_folder = find_job_workspace(job_id_or_name)

    detected_voice_id, detected_voice_label = resolve_job_voice(job_id_or_name, job_folder)

    # 1. Nếu đã có bản nháp, ưu tiên đọc bản nháp
    if draft_path.is_file():
        try:
            with open(draft_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["detected_voice_id"] = detected_voice_id
                data["detected_voice_label"] = detected_voice_label
                segs = data.get("segments", [])
                draft_changed = False

                all_default_hoaimy = all(s.get("voice_id") in [None, "", "microsoft-hoaimy"] for s in segs)
                if all_default_hoaimy and detected_voice_id != "microsoft-hoaimy":
                    for s in segs:
                        s["voice_id"] = detected_voice_id
                    draft_changed = True

                # Nếu các câu trong draft đều đang để tốc độ mặc định 1.0 (chưa từng được chỉnh thủ công),
                # cập nhật tốc độ thực tế đo được từ audio và text khớp với video
                all_flat_1_0 = all(float(s.get("speed") or 1.0) == 1.0 for s in segs)
                if all_flat_1_0 and data.get("revision", 1) <= 1:
                    for s in segs:
                        real_spd = estimate_segment_speed(
                            s.get("translated_text", ""),
                            float(s.get("audio_duration") or 0.0),
                            float(s.get("duration") or 0.0),
                        )
                        if real_spd != float(s.get("speed") or 1.0):
                            s["speed"] = real_spd
                            draft_changed = True

                if draft_changed:
                    try:
                        with open(draft_path, "w", encoding="utf-8") as fw:
                            json.dump(data, fw, ensure_ascii=False, indent=2)
                    except Exception as _phase6_exc:
                        logger.debug('Suppressed error in segment_editor_service.get_job_segments (open): %s', type(_phase6_exc).__name__, exc_info=False)
                return data
        except Exception as e:
            logger.warning("Không thể đọc draft %s: %s", draft_path, e)

    # 2. Tìm thư mục job trong workspace
    if not job_folder or not job_folder.is_dir():
        return {
            "job_id": clean_id,
            "revision": 1,
            "job_folder": None,
            "detected_voice_id": detected_voice_id,
            "detected_voice_label": detected_voice_label,
            "segments": [],
            "error": "Không tìm thấy thư mục xử lý của video này trong workspace."
        }

    segments: List[Dict[str, Any]] = []
    orig_segments_map: Dict[int, str] = {}

    # Case A: V2 segments.json (pipeline_v2/artifacts/tts/segments.json)
    v2_json_candidates = [
        job_folder / "pipeline_v2" / "artifacts" / "tts" / "segments.json",
        job_folder / "artifacts" / "tts" / "segments.json",
        job_folder / "tts" / "segments.json",
    ]
    v2_json = next((p for p in v2_json_candidates if p.is_file()), None)

    if v2_json:
        try:
            with open(v2_json, "r", encoding="utf-8") as f:
                v2_data = json.load(f)
                r_segs = v2_data.get("runtime_segments", [])
                meta_segs_map = {
                    s.get("index", s.get("source_segment_id")): s
                    for s in v2_data.get("segments", [])
                    if isinstance(s, dict)
                }
                tts_dir = v2_json.parent
                for seg in r_segs:
                    idx = seg.get("index", 1)
                    start_s = float(seg.get("start", 0.0))
                    end_s = float(seg.get("end", 0.0))
                    dur = round(end_s - start_s, 2)
                    orig = seg.get("orig_content") or seg.get("text") or ""
                    trans = seg.get("content") or ""

                    # Find segment audio
                    audio_p = tts_dir / f"{idx}.mp3"
                    audio_dur = get_audio_duration_seconds(audio_p) if audio_p.is_file() else 0.0
                    fits = audio_dur <= (dur + 0.3) if audio_dur > 0 else True

                    meta_seg = meta_segs_map.get(idx, {})
                    meta_speed = meta_seg.get("applied_atempo")
                    if not meta_speed and meta_seg.get("source_audio_duration") and meta_seg.get("actual_audio_duration"):
                        try:
                            s_dur = float(meta_seg["source_audio_duration"])
                            a_dur = float(meta_seg["actual_audio_duration"])
                            if a_dur > 0 and s_dur > 0:
                                meta_speed = s_dur / a_dur
                        except (ValueError, TypeError, ZeroDivisionError) as _phase6_exc:
                            logger.debug('Suppressed error in segment_editor_service.get_job_segments (float): %s', type(_phase6_exc).__name__, exc_info=False)

                    seg_speed = estimate_segment_speed(trans, audio_dur, dur, meta_speed=meta_speed)

                    segments.append({
                        "segment_id": f"seg_{idx}",
                        "index": idx,
                        "start": round(start_s, 2),
                        "end": round(end_s, 2),
                        "duration": dur,
                        "source_text": orig,
                        "translated_text": trans,
                        "voice_id": detected_voice_id,
                        "speed": seg_speed,
                        "audio_path": str(audio_p) if audio_p.is_file() else "",
                        "audio_url": f"/api/jobs/{clean_id}/segments/seg_{idx}/audio",
                        "audio_duration": audio_dur,
                        "fits_window": fits,
                        "status": "synced",
                    })
        except AudioDurationError:
            raise
        except Exception as e:
            logger.warning("Lỗi đọc V2 segments.json: %s", e)

    # Case B: V1 SRT files (original.srt & translated.srt)
    if not segments:
        orig_srt_candidates = [
            job_folder / "original.srt",
            job_folder / "pipeline_v2" / "artifacts" / "transcript" / "original.srt",
        ]
        trans_srt_candidates = [
            job_folder / "translated.srt",
            job_folder / "pipeline_v2" / "artifacts" / "translation" / "translated.srt",
        ]

        orig_srt = next((p for p in orig_srt_candidates if p.is_file()), None)
        trans_srt = next((p for p in trans_srt_candidates if p.is_file()), None)

        if orig_srt:
            try:
                for s in parse_srt(orig_srt.read_text(encoding="utf-8", errors="replace")):
                    orig_segments_map[s["index"]] = s.get("content", "")
            except Exception as _phase6_exc:
                logger.debug('Suppressed error in segment_editor_service.get_job_segments (parse_srt): %s', type(_phase6_exc).__name__, exc_info=False)

        if trans_srt:
            try:
                dubbing_dir_candidates = [
                    job_folder / "dubbing",
                    job_folder / "pipeline_v2" / "artifacts" / "tts",
                ]
                dub_dir = next((d for d in dubbing_dir_candidates if d.is_dir()), None)
                if not dub_dir:
                    nested_dubs = list(job_folder.rglob("dubbing")) + list(job_folder.rglob("tts"))
                    dub_dir = nested_dubs[0] if nested_dubs else (job_folder / "dubbing")

                for s in parse_srt(trans_srt.read_text(encoding="utf-8", errors="replace")):
                    idx = s["index"]
                    start_s = s["start"]
                    end_s = s["end"]
                    dur = s["duration"]
                    trans = s.get("content", "")
                    orig = orig_segments_map.get(idx, "")

                    audio_p = dub_dir / f"{idx}.mp3"
                    if not audio_p.is_file():
                        audio_p = dub_dir / f"{idx}.wav"
                    audio_dur = get_audio_duration_seconds(audio_p) if audio_p.is_file() else 0.0
                    fits = audio_dur <= (dur + 0.3) if audio_dur > 0 else True

                    seg_speed = estimate_segment_speed(trans, audio_dur, dur)

                    segments.append({
                        "segment_id": f"seg_{idx}",
                        "index": idx,
                        "start": round(start_s, 2),
                        "end": round(end_s, 2),
                        "duration": dur,
                        "source_text": orig,
                        "translated_text": trans,
                        "voice_id": detected_voice_id,
                        "speed": seg_speed,
                        "audio_path": str(audio_p) if audio_p.is_file() else "",
                        "audio_url": f"/api/jobs/{clean_id}/segments/seg_{idx}/audio",
                        "audio_duration": audio_dur,
                        "fits_window": fits,
                        "status": "synced",
                    })
            except AudioDurationError:
                raise
            except Exception as e:
                logger.warning("Lỗi đọc V1 SRT files: %s", e)

    result = {
        "job_id": clean_id,
        "revision": 1,
        "job_folder": str(job_folder),
        "total_segments": len(segments),
        "detected_voice_id": detected_voice_id,
        "detected_voice_label": detected_voice_label,
        "segments": segments,
        "original_snapshot": [dict(s) for s in segments],
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    # Save initial snapshot draft
    if segments:
        try:
            tmp = draft_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            os.replace(tmp, draft_path)
        except Exception as _phase6_exc:
            logger.debug('Suppressed error in segment_editor_service.get_job_segments (draft_path.with_suffix): %s', type(_phase6_exc).__name__, exc_info=False)

    return result


def save_segment_draft(
    job_id_or_name: str,
    updated_segments: List[Dict[str, Any]],
    expected_revision: int = 1,
) -> Dict[str, Any]:
    """
    Lưu bản nháp câu đã chỉnh sửa với cơ chế khóa lạc quan (optimistic locking).
    """
    clean_id = sanitize_job_id(job_id_or_name)
    draft_path = get_segment_drafts_dir() / f"{clean_id}.json"

    current_data = get_job_segments(job_id_or_name)
    current_rev = current_data.get("revision", 1)

    if expected_revision != current_rev:
        raise ValueError(
            f"Xung đột phiên bản (Revision Conflict): Dữ liệu hiện tại là phiên bản {current_rev}, "
            f"nhưng client gửi lên phiên bản {expected_revision}. Vui lòng làm mới trang."
        )

    new_rev = current_rev + 1

    # Update segments in current data
    seg_map = {s["segment_id"]: s for s in updated_segments}
    final_segments = []

    for s in current_data.get("segments", []):
        sid = s["segment_id"]
        if sid in seg_map:
            upd = seg_map[sid]
            new_text = (upd.get("translated_text") or "").strip()
            if not new_text:
                raise ValueError(
                    f"Câu {sid} không được để trống văn bản — hãy nhập nội dung trước khi lưu."
                )
            text_changed = new_text != s.get("translated_text", "")
            if text_changed and not os.environ.get("PYTEST_CURRENT_TEST"):
                # F7 — học từ sửa tay: diff ngắn mức từ sẽ thành gợi ý Glossary khi lặp lại
                try:
                    from glossary_learning_service import record_edit
                    record_edit(s.get("translated_text", ""), new_text, clean_id)
                except Exception as _phase6_exc:
                    logger.debug('Suppressed error in segment_editor_service.save_segment_draft (record_edit): %s', type(_phase6_exc).__name__, exc_info=False)
            s["translated_text"] = new_text
            if "voice_id" in upd:
                s["voice_id"] = upd["voice_id"]
            if "speed" in upd:
                s["speed"] = float(upd["speed"])
            if text_changed and s.get("status") != "regenerated":
                s["status"] = "modified"
        final_segments.append(s)

    current_data["segments"] = final_segments
    current_data["revision"] = new_rev
    current_data["updated_at"] = time.time()

    # Save atomic
    tmp = draft_path.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, draft_path)

    return {
        "status": "success",
        "job_id": clean_id,
        "revision": new_rev,
        "segments": final_segments,
    }


async def regenerate_segments(
    job_id_or_name: str,
    segment_ids: List[str],
    expected_revision: int = 1,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    """Prepare audio/SRT/draft together and publish the revision only after all writes succeed."""
    from segment_artifacts import commit_segment_files

    clean_id = sanitize_job_id(job_id_or_name)
    cache_key = (clean_id, idempotency_key)
    request_key = (expected_revision, tuple(sorted(segment_ids)))
    async with _segment_lock:
        now = time.time()
        if idempotency_key and cache_key in _IDEMPOTENCY_CACHE:
            cached_time, cached_request, cached_res = _IDEMPOTENCY_CACHE[cache_key]
            if now - cached_time < 300:
                if cached_request != request_key:
                    raise ValueError("Idempotency key đã dùng cho yêu cầu tạo lại câu khác.")
                return cached_res
        current_data = deepcopy(get_job_segments(job_id_or_name))
        current_rev = current_data.get("revision", 1)
        if expected_revision != current_rev:
            raise ValueError(f"Xung đột phiên bản: hiện tại rev {current_rev}, client gửi rev {expected_revision}.")
        job_folder = find_job_workspace(job_id_or_name)
        if not job_folder:
            raise RuntimeError("Không tìm thấy thư mục làm việc của job.")
        dub_dir_candidates = [job_folder / "dubbing", job_folder / "pipeline_v2" / "artifacts" / "tts", job_folder / "tts"]
        dub_dir = next((d for d in dub_dir_candidates if d.is_dir()), job_folder / "dubbing")
        dub_dir.mkdir(parents=True, exist_ok=True)
        segments = current_data.get("segments", [])
        target_ids = set(segment_ids)
        selected = [s for s in segments if s.get("segment_id") in target_ids]
        if not target_ids or {s.get("segment_id") for s in selected} != target_ids:
            raise ValueError("Danh sách câu không tồn tại đầy đủ trong revision hiện tại.")
        indices = [s.get("index") for s in selected]
        if any(not isinstance(i, int) or isinstance(i, bool) or i <= 0 for i in indices) or len(set(indices)) != len(indices):
            raise ValueError("Chỉ số audio segment không hợp lệ hoặc bị trùng.")
        if any(not (s.get("translated_text") or "").strip() for s in selected):
            raise ValueError("Câu đang để trống văn bản; chưa thể tạo âm thanh.")

        # Required backups are completed before any synthesis or replacement.
        for s in selected:
            destination = dub_dir / f"{s['index']}.mp3"
            backup = destination.with_suffix(".mp3.bak")
            if backup.exists() and (not backup.is_file() or backup.stat().st_size <= 0):
                raise SegmentCommitError("Bản backup audio cũ không hợp lệ; chưa tạo lại câu.")
            if destination.is_file() and not backup.exists():
                try:
                    atomic_copy_file(destination, backup)
                except Exception as exc:
                    raise SegmentCommitError("Không sao lưu được audio cũ; chưa tạo lại câu.") from exc

        stage = Path(tempfile.mkdtemp(prefix=".segment-regenerate-", dir=job_folder))
        keep_recovery = False
        try:
            from voice_preview_service import synthesize_voice_preview, get_preview_audio_path

            prepared = {}
            regen_results = []
            for s in selected:
                sid, idx = s["segment_id"], s["index"]
                voice_id = s.get("voice_id") or current_data.get("detected_voice_id") or "capcut-BV562_streaming"
                speed = float(s.get("speed") or 1.0)
                window_dur = float(s.get("duration") or 2.0)
                if not math.isfinite(speed) or speed <= 0 or not math.isfinite(window_dur) or window_dur <= 0:
                    raise ValueError("Tốc độ/khung thời gian segment không hợp lệ.")
                synth_res = await synthesize_voice_preview(voice_id=voice_id, text=s["translated_text"].strip(), speed=speed)
                source = get_preview_audio_path(synth_res["preview_id"])
                if not source or not source.is_file():
                    raise RuntimeError("Không tạo được audio preview cho câu.")
                raw_dur = get_audio_duration_seconds(source)
                staged_audio = stage / f"audio-{idx}.mp3"
                applied_atempo, fits, warning = 1.0, True, None
                if raw_dur > window_dur + 0.25 and window_dur > 0.5:
                    applied_atempo = round(min(2.0, max(0.5, raw_dur / window_dur)), 2)
                    fits = False
                    warning = f"Câu dài hơn khung thời gian ({raw_dur}s > {window_dur}s). Đã nén tốc độ {applied_atempo}x."
                    result = await asyncio.to_thread(subprocess.run, [
                        "ffmpeg", "-y", "-i", str(source), "-filter:a", f"atempo={applied_atempo:.2f}", str(staged_audio)
                    ], capture_output=True, text=True)
                    if result.returncode != 0 or not staged_audio.is_file():
                        raise RuntimeError("Nén audio theo khung thời gian thất bại; chưa ghi revision mới.")
                else:
                    shutil.copy2(source, staged_audio)
                final_dur = get_audio_duration_seconds(staged_audio)
                destination = dub_dir / f"{idx}.mp3"
                prepared[destination] = staged_audio
                s.update(audio_path=str(destination), audio_duration=final_dur, fits_window=fits, status="regenerated")
                regen_results.append({"segment_id": sid, "index": idx, "new_duration": final_dur,
                                      "fits_window": fits, "applied_atempo": applied_atempo, "warning": warning})

            new_srt_content = format_srt(segments)
            srt_stage = stage / "translated.srt"
            srt_stage.write_text(new_srt_content, encoding="utf-8")
            for destination in [job_folder / "translated.srt", job_folder / "pipeline_v2" / "artifacts" / "translation" / "translated.srt"]:
                if destination.parent.is_dir():
                    prepared[destination] = srt_stage
            new_rev = current_rev + 1
            current_data.update(revision=new_rev, segments=segments, updated_at=time.time())
            draft_path = get_segment_drafts_dir() / f"{clean_id}.json"
            draft_stage = stage / "draft.json"
            draft_stage.write_text(json.dumps(current_data, ensure_ascii=False, indent=2), encoding="utf-8")
            prepared[draft_path] = draft_stage
            commit_segment_files(prepared, stage / "rollback")

            result = {"status": "success", "job_id": clean_id, "revision": new_rev,
                      "regenerated_count": len(regen_results), "results": regen_results, "segments": segments}
            if idempotency_key:
                _IDEMPOTENCY_CACHE[cache_key] = (time.time(), request_key, result)
            return result
        except SegmentCommitError as exc:
            keep_recovery = bool(getattr(exc, "recovery_dir", None))
            raise
        finally:
            if not keep_recovery:
                try:
                    shutil.rmtree(stage)
                except OSError as exc:
                    logger.warning("Cannot clean segment staging directory: %s", type(exc).__name__, exc_info=False)



async def publish_revised_video(
    job_id_or_name: str,
    expected_revision: int = 1,
) -> Dict[str, Any]:
    """
    Xuất video thành phẩm bản sửa mới (Dubbed_..._rev{revision}.mp4).
    Bảo toàn bản cũ, hòa âm track lời mới và remux với video nguồn.
    """
    clean_id = sanitize_job_id(job_id_or_name)
    current_data = get_job_segments(job_id_or_name)
    current_rev = current_data.get("revision", 1)

    if expected_revision != current_rev:
        raise ValueError(f"Revision mismatch: Current rev is {current_rev}, client sent {expected_revision}")

    job_folder = find_job_workspace(job_id_or_name)
    if not job_folder:
        raise RuntimeError(f"Không tìm thấy thư mục làm việc cho job {clean_id}")

    out_dir = resolve_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Locate source video
    source_video_candidates = [
        out_dir / f"Dubbed_{clean_id}.mp4",
        out_dir / f"{clean_id}.mp4",
        job_folder / "pipeline_v2" / "artifacts" / "output" / "final.mp4",
        job_folder / "input.mp4",
        job_folder / f"{clean_id}.mp4",
        PATHS.input_dir / f"{clean_id}.mp4",
        PATHS.output_dir / f"Dubbed_{clean_id}.mp4",
    ]
    source_video = next((p for p in source_video_candidates if p.is_file()), None)

    if not source_video:
        # Check recursively in job_folder for any mp4
        mp4s = list(job_folder.rglob("*.mp4"))
        for cand in mp4s:
            if "final" in cand.name.lower() or "dubbed" in cand.name.lower() or "input" in cand.name.lower() or cand.name == f"{clean_id}.mp4":
                source_video = cand
                break
        if not source_video and mp4s:
            source_video = mp4s[0]

    if not source_video:
        # Check any matching in banve or video tool v2
        candidate_output_dirs = [resolve_output_dir()]
        if PATHS.shared_workspace_dir:
            candidate_output_dirs.append(PATHS.shared_workspace_dir / "output")
        for cand_dir in candidate_output_dirs:
            if cand_dir.is_dir():
                matches = list(cand_dir.glob(f"*{clean_id}*.mp4"))
                if matches:
                    source_video = matches[0]
                    break

    if not source_video:
        raise RuntimeError(f"Không tìm thấy video gốc để xuất bản sửa cho {clean_id}")

    # Output file name: Dubbed_{clean_id}_rev{current_rev}.mp4
    rev_filename = f"Dubbed_{clean_id}_rev{current_rev}.mp4"
    rev_filepath = out_dir / rev_filename
    # Xuất vào file tạm (staging) rồi mới đổi tên — file thành phẩm cũ không bao giờ hỏng giữa chừng
    staging_path = rev_filepath.with_name(rev_filename + f".part{uuid.uuid4().hex[:8]}")

    # Build audio track from segments using ffmpeg concat or overlay
    segments = current_data.get("segments", [])
    if not segments:
        raise RuntimeError("Không có segment nào để xuất video")

    dub_dir_candidates = [
        job_folder / "dubbing",
        job_folder / "pipeline_v2" / "artifacts" / "tts",
        job_folder / "tts",
    ]
    dub_dir = next((d for d in dub_dir_candidates if d.is_dir()), None)
    if not dub_dir:
        nested_dubs = list(job_folder.rglob("dubbing")) + list(job_folder.rglob("tts"))
        if nested_dubs:
            dub_dir = nested_dubs[0]
        else:
            dub_dir = job_folder / "dubbing"

    # Generate audio track
    concat_list_file = job_folder / f"concat_{uuid.uuid4().hex}.txt"
    merged_audio_file = job_folder / f"merged_dub_{uuid.uuid4().hex}.mp3"

    try:
        # Write concat list with audio segments
        with open(concat_list_file, "w", encoding="utf-8") as f:
            for s in sorted(segments, key=lambda x: x.get("index", 1)):
                idx = s.get("index", 1)
                seg_f = dub_dir / f"{idx}.mp3"
                if not seg_f.is_file():
                    seg_f = dub_dir / f"{idx}.wav"
                if seg_f.is_file():
                    safe_path = str(seg_f).replace("\\", "/")
                    f.write(f"file '{safe_path}'\n")

        # Concat audio
        cmd_concat = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_file),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(merged_audio_file),
        ]
        await asyncio.to_thread(subprocess.run, cmd_concat, capture_output=True, text=True)

        # Mux với video — ghi vào file tạm (staging); chỉ đổi tên thành file thành phẩm khi ffmpeg kết thúc sạch
        cmd_mux = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_video),
            "-i",
            str(merged_audio_file if merged_audio_file.is_file() else source_video),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-movflags",
            "+faststart",
            str(staging_path),
        ]
        p_mux = await asyncio.to_thread(subprocess.run, cmd_mux, capture_output=True, text=True)

        publish_ok = p_mux.returncode == 0 and staging_path.is_file() and staging_path.stat().st_size > 0
        if not publish_ok:
            # If -c:v copy failed (e.g. incompatible stream or format), fallback to transcode to standard H.264
            cmd_mux_reencode = [
                "ffmpeg",
                "-y",
                "-i",
                str(source_video),
                "-i",
                str(merged_audio_file if merged_audio_file.is_file() else source_video),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-movflags",
                "+faststart",
                str(staging_path),
            ]
            p_reencode = await asyncio.to_thread(subprocess.run, cmd_mux_reencode, capture_output=True, text=True)
            publish_ok = p_reencode.returncode == 0 and staging_path.is_file() and staging_path.stat().st_size > 0

        if not publish_ok:
            # Không còn fallback "copy video cũ + báo success" — trả lỗi rõ ràng, bản gốc giữ nguyên
            raise RuntimeError(
                "Không thể xuất video bản sửa (ffmpeg lỗi). File thành phẩm cũ được giữ nguyên nguyên vẹn."
            )

        os.replace(staging_path, rev_filepath)
    finally:
        for tmp_f in [concat_list_file, merged_audio_file, staging_path]:
            try:
                if tmp_f.is_file():
                    tmp_f.unlink()
            except OSError as _phase6_exc:
                logger.debug('Suppressed error in segment_editor_service.publish_revised_video (tmp_f.is_file): %s', type(_phase6_exc).__name__, exc_info=False)
        # Dọn file staging mồ côi từ các lần xuất bản đổ vỡ trước đó
        try:
            for stale in out_dir.glob(rev_filename + ".part*"):
                stale.unlink(missing_ok=True)
        except OSError as _phase6_exc:
            logger.debug('Suppressed error in segment_editor_service.publish_revised_video (out_dir.glob): %s', type(_phase6_exc).__name__, exc_info=False)

    # Tăng revision draft sau khi xuất bản thành công để lần xuất sau tạo file _rev(N+1) riêng
    published_rev = current_rev + 1
    try:
        draft_path = get_segment_drafts_dir() / f"{clean_id}.json"
        current_data["revision"] = published_rev
        current_data["published_at"] = time.time()
        tmp_draft = draft_path.with_suffix(f".tmp{os.getpid()}")
        with open(tmp_draft, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_draft, draft_path)
    except Exception as draft_err:
        logger.warning("Không thể tăng revision draft sau xuất bản %s: %s", clean_id, draft_err)

    return {
        "status": "success",
        "job_id": clean_id,
        "revision": published_rev,
        "published_filename": rev_filename,
        "published_path": str(rev_filepath),
        "published_size_mb": round(rev_filepath.stat().st_size / (1024 * 1024), 2),
        "view_url": f"/api/view/{rev_filename}",
    }


def get_segment_audio_path(job_id_or_name: str, segment_id: str) -> Optional[Path]:
    """Tìm file audio riêng của một segment để phát trên player."""
    clean_id = sanitize_job_id(job_id_or_name)
    job_folder = find_job_workspace(job_id_or_name)
    if not job_folder:
        return None

    # Parse index from seg_X
    m = re.search(r"\d+", str(segment_id))
    idx = int(m.group(0)) if m else 1

    dub_dir_candidates = [
        job_folder / "dubbing",
        job_folder / "pipeline_v2" / "artifacts" / "tts",
        job_folder / "tts",
    ]
    for d in dub_dir_candidates:
        if d.is_dir():
            target = d / f"{idx}.mp3"
            if target.is_file() and target.stat().st_size > 64:
                return target
            target_wav = d / f"{idx}.wav"
            if target_wav.is_file() and target_wav.stat().st_size > 64:
                return target_wav

    # Check nested subdirectories recursively
    for cand in job_folder.rglob(f"{idx}.mp3"):
        if cand.is_file() and cand.stat().st_size > 64:
            return cand
    for cand in job_folder.rglob(f"{idx}.wav"):
        if cand.is_file() and cand.stat().st_size > 64:
            return cand

    return None
