# -*- coding: utf-8 -*-
"""AI Scriptwriting engine based on social-media-skills patterns (reels-scripting & hook-generator)."""

from __future__ import annotations

import json
import logging
import os
import re
import base64
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests
import time
import cv2
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# .env được nạp bởi main.py khi khởi động server.
# Nếu chạy module standalone, cần đặt GEMINI_API_KEY trong môi trường.


TIMELINE_REPAIR_TOLERANCE = 0.5  # Ngưỡng cho phép auto-repair chênh lệch float (<= 0.5s); vượt quá sẽ reject


class ScriptScene(BaseModel):
    index: int
    section: str = "body"
    speaker_text: str
    visual_cue: str = ""
    text_overlay: str = ""

    # 1. Timeline ngân sách video (Video budget timeline)
    planned_start: Optional[float] = None
    planned_end: Optional[float] = None
    target_duration: Optional[float] = None

    # 2. Ước lượng trước TTS dựa trên số từ (Estimated duration)
    estimated_duration: Optional[float] = None

    # 3. Kết quả đo đạc audio thực tế sau TTS (Actual TTS measurement)
    actual_start: Optional[float] = None
    actual_end: Optional[float] = None
    actual_duration: Optional[float] = None
    duration_delta: Optional[float] = None

    # Metadata Closed-Loop TTS
    rewrite_attempts: int = 0
    duration_status: str = "pending"  # "pending" | "pass" | "best_effort"

    # Backward compatibility aliases
    start_seconds: Optional[float] = None
    end_seconds: Optional[float] = None
    time_range: Optional[str] = None
    word_count: Optional[int] = None
    duration_seconds: Optional[float] = None


class GeneratedScript(BaseModel):
    title: str
    topic: Optional[str] = None
    estimated_total_seconds: Optional[float] = None
    video_duration: Optional[float] = None
    detected_subject: Optional[str] = None
    genre: Optional[str] = None
    caption: Optional[str] = None
    hashtags: Optional[List[str]] = None
    scenes: List[ScriptScene]


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


DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_SCRIPT_MODEL = os.getenv("GEMINI_SCRIPT_MODEL", DEFAULT_GEMINI_MODEL).strip()
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", DEFAULT_GEMINI_MODEL).strip()
GEMINI_REWRITE_MODEL = os.getenv("GEMINI_REWRITE_MODEL", DEFAULT_GEMINI_MODEL).strip()
GEMINI_HOOK_MODEL = os.getenv("GEMINI_HOOK_MODEL", DEFAULT_GEMINI_MODEL).strip()

GEMINI_FALLBACK_MODELS = [
    DEFAULT_GEMINI_MODEL,
    GEMINI_SCRIPT_MODEL,
    GEMINI_VISION_MODEL,
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-flash-lite-latest",
]


def calculate_sample_timestamps(duration_seconds: float, max_frames: int = 8) -> List[float]:
    """
    Tính toán các mốc thời gian lấy mẫu khung hình video:
    - Bắt buộc có 1 frame rất gần đầu video (<= 0.5s)
    - Bắt buộc có frame trong vùng Hook (0 - 3s)
    - Các frame trải đều thân video
    - 1 frame gần cuối video
    - Dedupe với khoảng cách an toàn, không bao giờ vượt duration_seconds.
    - Xử lý an toàn các biên: duration ngắn, max_frames=1,2,3, không bị ZeroDivisionError.
    """
    if duration_seconds is None or duration_seconds <= 0:
        raise ValueError("Thời lượng video phải lớn hơn 0 (duration_seconds > 0).")
    if max_frames is None or max_frames < 1:
        raise ValueError("Số lượng khung hình lấy mẫu phải >= 1 (max_frames >= 1).")

    dur = float(duration_seconds)
    if max_frames == 1:
        return [round(min(0.3, dur * 0.5), 2)]

    if dur <= 1.0:
        if max_frames == 1:
            return [round(dur * 0.5, 2)]
        t1 = round(dur * 0.2, 2)
        t2 = round(dur * 0.8, 2)
        return [t1, t2] if t1 != t2 else [round(dur * 0.5, 2)]

    candidates = []
    # 1. Khung đầu tiên rất gần đầu video (<= 0.5s)
    candidates.append(min(0.3, round(dur * 0.1, 2)))

    # 2. Khung trong vùng Hook (0 - 3s)
    if dur >= 4.0:
        candidates.append(1.5)
        candidates.append(2.8)
    elif dur >= 2.5:
        candidates.append(round(dur * 0.5, 2))

    # 3. Khung thân video (từ 3s tới gần cuối)
    if dur > 5.0:
        start_body = 3.0 if dur > 6.0 else dur * 0.5
        for pct in [0.25, 0.45, 0.65, 0.85]:
            t = round(dur * pct, 2)
            if start_body <= t <= dur - 0.5:
                candidates.append(t)

    # 4. Khung gần cuối video
    candidates.append(round(max(0.1, dur - 0.3), 2))

    # Lọc và dedupe (khoảng cách tối thiểu 0.25s)
    valid = sorted([round(t, 2) for t in candidates if 0.0 <= t <= dur])
    deduped = []
    for t in valid:
        if not deduped or (t - deduped[-1]) >= 0.25:
            deduped.append(t)

    # Giới hạn max_frames (giữ đầu và cuối)
    if len(deduped) > max_frames:
        if max_frames == 1:
            return [deduped[0]]
        step = (len(deduped) - 1) / (max_frames - 1)
        indices = [int(round(i * step)) for i in range(max_frames)]
        deduped = [deduped[i] for i in sorted(set(indices))]

    return deduped


def build_scene_timeline(
    duration_seconds: float,
    hook_duration: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Xây dựng timeline phân cảnh động dựa vào thời lượng thực tế của video:
    - Hỗ trợ hook_duration tùy chỉnh (5s - 10s né bản quyền và thu hút giữ chân).
    - Invariant: 0 <= start_seconds < end_seconds <= duration_seconds
    - Các cảnh liên tục nhau và cảnh cuối cùng kết thúc tại duration_seconds
    """
    dur = round(float(duration_seconds), 1)
    if hook_duration is None:
        if dur <= 5.0:
            half = round(dur * 0.5, 1)
            cuts = [0.0, half, dur]
            sections = ['hook', 'cta']
        elif dur <= 16.0:
            t_hook = min(2.5, round(dur * 0.2, 1))
            t_cta = round(dur - min(3.0, dur * 0.25), 1)
            mid = round((t_hook + t_cta) / 2, 1)
            cuts = [0.0, t_hook, mid, t_cta, dur]
            sections = ['hook', 'point_1', 'point_2', 'cta']
        elif dur <= 35.0:
            t_hook = 3.0
            t_cta = round(dur - min(6.0, dur * 0.2), 1)
            t_mid = round(t_hook + (t_cta - t_hook) * 0.45, 1)
            cuts = [0.0, t_hook, t_mid, t_cta, dur]
            sections = ['hook', 'point_1', 'point_2', 'cta']
        elif dur <= 55.0:
            t_hook = 3.0
            t_cta = round(dur - min(8.0, dur * 0.18), 1)
            t_p1 = round(t_hook + (t_cta - t_hook) * 0.4, 1)
            cuts = [0.0, t_hook, t_p1, t_cta, dur]
            sections = ['hook', 'point_1', 'point_2', 'cta']
        else:  # > 55s
            t_hook = 3.0
            t_cta = round(dur - min(10.0, dur * 0.15), 1)
            rem = t_cta - t_hook
            t_p1 = round(t_hook + rem * 0.32, 1)
            t_p2 = round(t_hook + rem * 0.68, 1)
            cuts = [0.0, t_hook, t_p1, t_p2, t_cta, dur]
            sections = ['hook', 'point_1', 'point_2', 'point_3', 'cta']
    else:
        # Chế độ Hook tùy chỉnh (5s - 10s chuyên dụng né bản quyền & giữ chân)
        h_val = float(hook_duration)
        if dur <= 5.0:
            half = round(dur * 0.5, 1)
            cuts = [0.0, half, dur]
            sections = ['hook', 'cta']
        else:
            t_cta_len = round(min(8.0, max(2.5, dur * 0.18)), 1)
            max_h = round(dur - t_cta_len - 1.5, 1)
            t_hook = round(max(2.0, min(h_val, max_h)), 1)
            t_cta = round(dur - t_cta_len, 1)
            rem = round(t_cta - t_hook, 1)
            if rem <= 3.5:
                cuts = [0.0, t_hook, dur]
                sections = ['hook', 'cta']
            elif dur <= 20.0 or rem <= 8.0:
                mid = round((t_hook + t_cta) / 2, 1)
                cuts = [0.0, t_hook, mid, t_cta, dur]
                sections = ['hook', 'point_1', 'point_2', 'cta']
            elif dur <= 45.0 or rem <= 20.0:
                p1 = round(t_hook + rem * 0.45, 1)
                cuts = [0.0, t_hook, p1, t_cta, dur]
                sections = ['hook', 'point_1', 'point_2', 'cta']
            else:
                p1 = round(t_hook + rem * 0.33, 1)
                p2 = round(t_hook + rem * 0.67, 1)
                cuts = [0.0, t_hook, p1, p2, t_cta, dur]
                sections = ['hook', 'point_1', 'point_2', 'point_3', 'cta']

    scenes = []
    for i in range(len(cuts) - 1):
        s = cuts[i]
        e = cuts[i + 1]
        mm_s, ss_s = int(s // 60), s % 60
        mm_e, ss_e = int(e // 60), e % 60
        t_dur = round(e - s, 2)
        sc_dict = {
            'index': i + 1,
            'section': sections[i],
            'planned_start': s,
            'planned_end': e,
            'target_duration': t_dur,
            'estimated_duration': t_dur,
            'start_seconds': s,
            'end_seconds': e,
            'duration_seconds': t_dur,
            'time_range': f"{mm_s:02d}:{ss_s:04.1f} - {mm_e:02d}:{ss_e:04.1f}"
        }
        if sections[i] == 'hook':
            sc_dict['is_anti_copyright'] = (hook_duration is not None and hook_duration >= 4.5)
        scenes.append(sc_dict)
    return scenes


def parse_gemini_json(raw_text: str) -> Dict[str, Any]:
    """Parse JSON từ Gemini an toàn: thử json.loads trực tiếp trước, fallback regex sau."""
    if not raw_text:
        raise ValueError("Phản hồi từ Gemini rỗng.")
    text = raw_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception as e:
            raise ValueError(f"Không thể giải mã JSON từ phản hồi: {e}")
    raise ValueError("Không tìm thấy cấu trúc JSON hợp lệ trong phản hồi Gemini.")


def validate_and_normalize_script(
    data: Dict[str, Any],
    video_duration: Optional[float] = None
) -> Dict[str, Any]:
    """
    Validate cấu trúc kịch bản theo schema bắt buộc, kiểm tra timeline invariants:
    - Bắt buộc 'scenes' là danh sách không rỗng (scenes >= 1).
    - Bắt buộc 'speaker_text' không rỗng cho từng phân cảnh.
    - Validate mốc thời gian: 0 <= start_seconds < end_seconds.
    - Reject nếu start_seconds âm (< -0.1) hoặc start >= end.
    - Nếu có video_duration:
      * Reject nếu start_seconds > video_duration hoặc end_seconds > video_duration + 0.5.
      * Scene 1 bắt đầu tại 0.0.
      * Scene cuối kết thúc tại video_duration.
      * Tự động căn chỉnh liên tục (contiguous: scene[i+1].start = scene[i].end) nếu có sai lệch nhỏ/overlap.
    - Chuẩn hóa index liên tục 1..N.
    """
    if not isinstance(data, dict):
        raise ValueError("Dữ liệu kịch bản không phải là JSON object hợp lệ.")

    title = str(data.get("title") or "Kịch bản video ngắn").strip()
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) == 0:
        raise ValueError("Kịch bản phải chứa ít nhất 1 phân cảnh ('scenes').")

    validated_scenes = []
    dur = float(video_duration) if video_duration and video_duration > 0 else None

    for idx, raw_sc in enumerate(raw_scenes):
        if not isinstance(raw_sc, dict):
            raise ValueError(f"Phân cảnh #{idx + 1} phải là một JSON object.")
        text = str(raw_sc.get("speaker_text") or "").strip()
        if not text:
            raise ValueError(f"Phân cảnh #{idx + 1} có lời thoại trống.")

        words = len(text.split())
        start_sec = raw_sc.get("start_seconds")
        end_sec = raw_sc.get("end_seconds")

        if start_sec is not None:
            try:
                start_sec = float(start_sec)
            except (ValueError, TypeError):
                raise ValueError(f"Phân cảnh #{idx + 1} có start_seconds không hợp lệ: {start_sec}")
            if start_sec < -0.1:
                raise ValueError(f"Phân cảnh #{idx + 1} có start_seconds âm ({start_sec}s).")
            start_sec = max(0.0, round(start_sec, 2))

        if end_sec is not None:
            try:
                end_sec = float(end_sec)
            except (ValueError, TypeError):
                raise ValueError(f"Phân cảnh #{idx + 1} có end_seconds không hợp lệ: {end_sec}")
            end_sec = round(end_sec, 2)

        if start_sec is not None and end_sec is not None:
            if start_sec >= end_sec:
                raise ValueError(f"Phân cảnh #{idx + 1} có timeline không hợp lệ: start_seconds ({start_sec}s) >= end_seconds ({end_sec}s).")
            if dur is not None:
                if start_sec > dur:
                    raise ValueError(f"Phân cảnh #{idx + 1} có start_seconds ({start_sec}s) vượt quá thời lượng video ({dur}s).")
                if end_sec > dur + 0.5:
                    raise ValueError(f"Phân cảnh #{idx + 1} có end_seconds ({end_sec}s) vượt quá thời lượng video ({dur}s).")

        calc_duration = round(max(words / 2.4, 1.5), 1)
        if start_sec is not None and end_sec is not None:
            calc_duration = round(end_sec - start_sec, 2)

        scene_obj = ScriptScene(
            index=idx + 1,
            section=str(raw_sc.get("section") or "body").strip(),
            speaker_text=text,
            visual_cue=str(raw_sc.get("visual_cue") or "").strip(),
            text_overlay=str(raw_sc.get("text_overlay") or "").strip(),
            start_seconds=start_sec,
            end_seconds=end_sec,
            time_range=str(raw_sc.get("time_range") or "").strip() or None,
            word_count=words,
            duration_seconds=calc_duration
        )
        validated_scenes.append(scene_obj)

    if not validated_scenes:
        raise ValueError("Không có phân cảnh hợp lệ nào sau khi kiểm duyệt.")

    # Timeline contiguous repair & enforcement if dur is provided
    if dur is not None:
        has_full_timeline = all(s.start_seconds is not None and s.end_seconds is not None for s in validated_scenes)
        if has_full_timeline:
            # Check chronological order and reject large gaps/overlaps
            for i in range(len(validated_scenes) - 1):
                curr_s = validated_scenes[i]
                next_s = validated_scenes[i + 1]
                if next_s.start_seconds < curr_s.start_seconds:
                    raise ValueError(
                        f"Thứ tự thời gian phân cảnh bị đảo lộn: cảnh #{curr_s.index} "
                        f"({curr_s.start_seconds}s) đứng trước cảnh #{next_s.index} "
                        f"({next_s.start_seconds}s)."
                    )
                diff = next_s.start_seconds - curr_s.end_seconds
                if abs(diff) > TIMELINE_REPAIR_TOLERANCE:
                    raise ValueError(
                        f"Phân cảnh #{next_s.index} có timeline lệch quá lớn so với cảnh #{curr_s.index} "
                        f"(sai lệch: {abs(diff):.2f}s > ngưỡng {TIMELINE_REPAIR_TOLERANCE}s). "
                        f"Cảnh #{curr_s.index} kết thúc tại {curr_s.end_seconds}s, nhưng cảnh #{next_s.index} bắt đầu tại {next_s.start_seconds}s."
                    )
                # Auto-harmonize minor discrepancies (<= TIMELINE_REPAIR_TOLERANCE)
                if abs(diff) > 0.001:
                    next_s.start_seconds = curr_s.end_seconds

            # Invariant: Scene 1 starts at 0.0
            if validated_scenes[0].start_seconds > TIMELINE_REPAIR_TOLERANCE:
                raise ValueError(
                    f"Cảnh đầu tiên không bắt đầu từ 0 ({validated_scenes[0].start_seconds}s > {TIMELINE_REPAIR_TOLERANCE}s)."
                )
            validated_scenes[0].start_seconds = 0.0

            # Invariant: Last scene ends at dur (check if last scene ended too early)
            if (dur - validated_scenes[-1].end_seconds) > 2.0:
                raise ValueError(
                    f"Cảnh cuối cùng kết thúc tại {validated_scenes[-1].end_seconds}s, thiếu hụt lớn so với video ({dur}s)."
                )
            validated_scenes[-1].end_seconds = round(dur, 2)

            for sc in validated_scenes:
                if sc.end_seconds <= sc.start_seconds:
                    sc.end_seconds = round(sc.start_seconds + max(1.0, sc.duration_seconds or 1.5), 2)
                t_dur = round(sc.end_seconds - sc.start_seconds, 2)
                sc.planned_start = sc.start_seconds
                sc.planned_end = sc.end_seconds
                sc.target_duration = t_dur
                sc.duration_seconds = t_dur
                sc.estimated_duration = round(sc.word_count / 2.4, 1)
                mm_s, ss_s = int(sc.start_seconds // 60), sc.start_seconds % 60
                mm_e, ss_e = int(sc.end_seconds // 60), sc.end_seconds % 60
                sc.time_range = f"{mm_s:02d}:{ss_s:04.1f} - {mm_e:02d}:{ss_e:04.1f}"
    else:
        for sc in validated_scenes:
            if sc.start_seconds is not None and sc.end_seconds is not None:
                sc.planned_start = sc.start_seconds
                sc.planned_end = sc.end_seconds
                sc.target_duration = round(sc.end_seconds - sc.start_seconds, 2)
            else:
                sc.target_duration = sc.duration_seconds
            sc.estimated_duration = round(sc.word_count / 2.4, 1)

    # Ensure sequential 1..N indices
    for i, sc in enumerate(validated_scenes):
        sc.index = i + 1

    total_est = dur if dur else round(sum(s.duration_seconds or 2.0 for s in validated_scenes), 1)

    result = {
        "title": title,
        "topic": data.get("topic"),
        "estimated_total_seconds": data.get("estimated_total_seconds") or total_est,
        "video_duration": dur or data.get("video_duration"),
        "detected_subject": data.get("detected_subject"),
        "speaker_gender": data.get("speaker_gender") or "unknown",
        "speaker_pronoun": data.get("speaker_pronoun") or "minh",
        "genre": data.get("genre"),
        "caption": data.get("caption"),
        "hashtags": data.get("hashtags") or [],
        "scenes": [s.dict() for s in validated_scenes]
    }
    return result


def get_gemini_api_key(api_key: Optional[str] = None) -> str:
    return (api_key or os.getenv("GEMINI_API_KEY", "")).strip()


# JSON Schemas cho Structured Output của Gemini API
GENERATED_SCRIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "topic": {"type": "STRING"},
        "estimated_total_seconds": {"type": "NUMBER"},
        "video_duration": {"type": "NUMBER"},
        "detected_subject": {"type": "STRING"},
        "speaker_gender": {"type": "STRING"},
        "speaker_pronoun": {"type": "STRING"},
        "genre": {"type": "STRING"},
        "caption": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "scenes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "section": {"type": "STRING"},
                    "start_seconds": {"type": "NUMBER"},
                    "end_seconds": {"type": "NUMBER"},
                    "time_range": {"type": "STRING"},
                    "speaker_text": {"type": "STRING"},
                    "visual_cue": {"type": "STRING"},
                    "text_overlay": {"type": "STRING"}
                },
                "required": ["index", "speaker_text", "start_seconds", "end_seconds"]
            }
        }
    },
    "required": ["title", "scenes"]
}

SCENE_REWRITE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "speaker_text": {"type": "STRING"},
        "visual_cue": {"type": "STRING"},
        "text_overlay": {"type": "STRING"},
        "word_count": {"type": "INTEGER"}
    },
    "required": ["speaker_text"]
}

HOOKS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "type": {"type": "STRING"},
            "angle": {"type": "STRING"},
            "hook": {"type": "STRING"},
            "overlay_text": {"type": "STRING"},
            "visual_match": {"type": "NUMBER"},
            "visual_potential": {"type": "NUMBER"},
            "curiosity": {"type": "NUMBER"},
            "credibility": {"type": "NUMBER"},
            "specificity": {"type": "NUMBER"}
        },
        "required": ["type", "angle", "hook"]
    }
}


def call_gemini(
    prompt: Optional[str] = None,
    parts: Optional[List[Dict[str, Any]]] = None,
    api_key: Optional[str] = None,
    system_instruction: str = "",
    response_schema: Optional[Dict[str, Any]] = None,
    structured_output: bool = True
) -> Optional[str]:
    """Gọi Google Gemini API (hỗ trợ cả văn bản thuần túy và Multimodal Vision) với chính sách retry và Structured Output thông minh."""
    key = get_gemini_api_key(api_key)
    if not key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trong hệ thống!")

    models_to_try = []
    for m in GEMINI_FALLBACK_MODELS:
        if m and m not in models_to_try:
            models_to_try.append(m)

    content_parts = parts if parts is not None else [{"text": prompt or ""}]
    
    generation_config: Dict[str, Any] = {
        "responseMimeType": "application/json"
    }
    if structured_output and response_schema:
        generation_config["responseSchema"] = response_schema

    payload: Dict[str, Any] = {
        "contents": [{"parts": content_parts}],
        "generationConfig": generation_config
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": key,
    }
    last_error = None
    transient_503_count = 0

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        max_attempts = 2  # Thử tối đa 2 lần cho mỗi model nếu gặp 503 (quá tải tạm thời) hoặc 429
        for attempt in range(max_attempts):
            try:
                logger.info(f"Đang gọi Gemini ({model}) [lần {attempt + 1}/{max_attempts}]...")
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        return text
                    except (KeyError, IndexError) as e:
                        last_error = f"Lỗi cấu trúc phản hồi từ {model}: {data}"
                        logger.warning(last_error)
                        break
                elif resp.status_code == 400 and structured_output and response_schema:
                    # Thử fallback không có responseSchema (chế độ JSON tự do) nếu model từ chối schema
                    logger.warning(f"Model {model} từ chối responseSchema. Thử lại không kèm schema...")
                    fb_payload = {
                        **payload,
                        "generationConfig": {"responseMimeType": "application/json"}
                    }
                    resp_fb = requests.post(url, json=fb_payload, headers=headers, timeout=60)
                    if resp_fb.status_code == 200:
                        data = resp_fb.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    err_msg = f"HTTP {resp_fb.status_code}: {resp_fb.text}"
                    logger.error(f"Lỗi client xác thực / payload từ Gemini ({model}): {err_msg}")
                    raise RuntimeError(f"Lỗi xác thực / cấu hình Gemini API ({resp_fb.status_code}): {resp_fb.text}")
                elif resp.status_code in (400, 401, 403):
                    # Lỗi client (bad request, sai API key, không có quyền). Không retry các model khác vô ích!
                    err_msg = f"HTTP {resp.status_code}: {resp.text}"
                    logger.error(f"Lỗi client xác thực / payload từ Gemini ({model}): {err_msg}")
                    raise RuntimeError(f"Lỗi xác thực / cấu hình Gemini API ({resp.status_code}): {resp.text}")
                elif resp.status_code in (429, 503):
                    transient_503_count += 1
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
                    if attempt < max_attempts - 1:
                        backoff_sec = 1.5 * (attempt + 1)
                        logger.warning(f"Gemini {model} báo quá tải tạm thời (HTTP {resp.status_code}). Đang chờ {backoff_sec}s rồi thử lại...")
                        time.sleep(backoff_sec)
                        continue
                    else:
                        logger.warning(f"Gemini {model} quá tải sau {max_attempts} lần thử. Chuyển sang model fallback kế tiếp...")
                        break
                else:
                    # 404 (model không hỗ trợ), 5xx khác -> thử model dự phòng tiếp theo
                    last_error = f"HTTP {resp.status_code}: {resp.text}"
                    logger.warning(f"Lỗi gọi Gemini {model}: {last_error}. Đang thử model fallback tiếp theo...")
                    break
            except requests.RequestException as e:
                last_error = str(e)
                logger.warning(f"Ngoại lệ kết nối {model}: {e}")
                if attempt < max_attempts - 1:
                    time.sleep(1.0)
                    continue
                break

    if transient_503_count > 0:
        friendly_msg = (
            "Máy chủ Google Gemini đang gặp lưu lượng truy cập cao đột biến (HTTP 503 / 429 Quá tải tạm thời). "
            "Hệ thống đã tự động thử các model dự phòng và retry nhưng cụm server Google vẫn đang bận. "
            "Vui lòng đợi 5-10 giây rồi bấm thử lại!"
        )
        raise RuntimeError(f"{friendly_msg}\n(Chi tiết: {last_error})")

    raise RuntimeError(f"Không thể kết nối tới Google Gemini sau các model thử nghiệm. Lỗi: {last_error}")


HUMAN_VOICE_GUIDELINES = """
=== QUY TẮC BẮT BUỘC: LỜI VĂN 100% GẦN GŨI, TỰ NHIÊN NHƯ NGƯỜI THẬT (CHỐNG MÁY MÓC / LOẠI BỎ MÙI AI) ===

1. CÁCH NÓI CHUYỆN NGOÀI ĐỜI (CONVERSATIONAL & RELATABLE):
   - Đóng vai một người bạn thân ngoài đời đang trò chuyện hoặc một nhà sáng tạo nội dung TikTok/Reels dí dỏm, chân thành.
   - Xưng hô thân mật & đúng giới tính nhân vật:
     * Con trai (Nam): Xưng "anh" (gọi em / bạn / các bạn), hoặc xưng "mình". TUYỆT ĐỐI CẤM tự xưng "em" khi đang đóng vai người dẫn chuyện/đàn anh!
     * Con gái (Nữ): Xưng "em" (gọi anh/chị / mọi người / các bác), hoặc xưng "mình", "tui" (gọi mấy bà). TUYỆT ĐỐI CẤM tự xưng "anh"!
     * Giữ ngôi xưng NHẤT QUÁN 100% từ đầu đến cuối video, không hoán đổi giữa các phân cảnh.
   - Dùng các khẩu ngữ, trợ từ cảm thán tự nhiên của người Việt: "Trời ơi", "Ủa", "Nói thật là...", "Thề luôn...", "Nhìn mê chưa nè", "Bác nào hay bị...", "Cái này đỉnh ở chỗ...", "Tưởng không ngon mà ngon không tưởng...", "Ưng cái bụng liền", "Đỡ tốn bao nhiêu thời gian".

2. TUYỆT ĐỐI CẤM CÁC TỪ NGỮ VĂN BẢN / SÁCH VỞ / RẬP KHUÔN MÁY MÓC CỦA AI:
   - CẤM: "chúng ta", "quý vị", "người tiêu dùng", "khách hàng", "tôi xin giới thiệu", "hãy cùng tôi khám phá", "trong thời đại công nghệ số ngày nay", "như chúng ta đã biết", "mang lại giải pháp tối ưu", "hiệu quả vượt trội", "thiết kế tinh xảo", "đáp ứng mọi nhu cầu", "sự lựa chọn hoàn hảo", "vô cùng quan trọng".
   - CẤM các câu giảng đạo lý sáo rỗng. Hãy đi thẳng vào cảm xúc, cảm giác cầm nắm, trải nghiệm dùng thử hoặc sự việc cụ thể trước mắt!

3. NHỊP CÂU NGẮN - NGẮT NGHỈ TỰ NHIÊN CHO GIỌNG ĐỌC TTS:
   - Mỗi câu chỉ dài 5 đến 12 từ. Dùng dấu phẩy (,) và dấu chấm (.) để ngắt nhịp thở rõ ràng.
   - Tránh câu ghép dài ngoằng khiến công cụ Text-To-Speech (CapCut Mai, Nam Trầm, Thanh Niên) đọc bị hết hơi hoặc dồn dập.

4. KÊU GỌI HÀNH ĐỘNG (CTA) CHÂN THẬT, KHÔNG HÔ HÀO QUẢNG CÁO:
   - Thay vì "Hãy nhanh tay đặt mua ngay hôm nay để nhận ưu đãi", hãy dùng: "Bác nào cần thì link mình để góc dưới nha", "Bà nào thích thì cmt mình gửi chỗ mua", "Lưu lại liền kẻo lúc cần tìm lại không thấy nha!".
"""

ANTI_COPYRIGHT_HOOK_GUIDELINES = """
=== QUY TẮC BẮT BUỘC: HOOK 5-10 GIÂY ĐẦU NÉ BẢN QUYỀN & THU HÚT NGƯỜI XEM (VIRAL RETENTION) ===

1. MỤC TIÊU NÉ QUÉT BẢN QUYỀN (ALGORITHMIC COPYRIGHT EVASION / FAIR USE):
   - 5-10 giây đầu tiên là 'cửa sổ vàng' mà thuật toán kiểm duyệt (TikTok, Meta Reels, YouTube Shorts) quét đối chiếu video và audio fingerprint.
   - BẮT BUỘC lời thoại Hook (Scene 1) phải phủ kín hoàn toàn từ 0.0s đến 5.0s - 10.0s, đọc trôi chảy, nhịp câu cuốn hút, TUYỆT ĐỐI KHÔNG ĐỂ KHOẢNG LẶNG ĐỂ LỘ ÂM THANH GỐC.
   - BẮT BUỘC BẺ LÁI NGỮ CẢNH (RE-FRAMING): Không thuật lại đơn điệu những gì đang diễn ra, mà phải đặt một góc nhìn hoàn toàn mới (bình luận độc quyền, phản ứng bất ngờ, vạch trần chi tiết ít ai thấy, cảnh báo sai lầm, hoặc kể một câu chuyện ly kỳ) để biến video thành nội dung sáng tạo mới 100% (Fair Use).

2. MỤC TIÊU THU HÚT NGƯỜI XEM (PATTERN INTERRUPT & CURIOSITY LOOP):
   - Ngay 3 giây đầu: Dùng câu giật gân, đánh trúng tâm lý tò mò hoặc nỗi sợ/lợi ích ("Khoan lướt qua!", "Dừng lại 3 giây!", "Cảnh báo khẩn cấp!", "Ủa alo thề luôn...", "Bí mật mà các shop giấu bạn...").
   - Giây 4 đến 8/10: Mở một 'Curiosity Loop' (vòng lặp tò mò chưa giải đáp), buộc người xem phải xem tiếp thân bài hoặc xem đến cuối video ("Xem đến giây thứ 8 bạn sẽ hiểu tại sao...", "Chi tiết ở góc màn hình sẽ làm bạn ngã ngửa...", "Đừng làm theo kẻo tiền mất tật mang nha!").

3. ĐỘ DÀI LỜI THOẠI CHUẨN HOÁ CHO HOOK 5-10S:
   - Hook 5 giây: Khoảng 12 - 14 từ.
   - Hook 7 giây: Khoảng 16 - 19 từ.
   - Hook 10 giây: Khoảng 22 - 26 từ.
   - Viết thành 1 đến 2 câu ngắn, dùng dấu phẩy ngắt nhịp thở rõ ràng cho giọng đọc AI (CapCut).

4. TEXT OVERLAY ĐẬP VÀO MẮT (VISUAL ANTI-FINGERPRINT BANNER):
   - Text Overlay của Hook BẮT BUỘC viết IN HOA, từ 3 đến 6 từ, chứa từ khóa mạnh hoặc emoji cảnh báo (ví dụ: '🚨 DỪNG LẠI 3S KẺO MẤT TIỀN', '😱 SỰ THẬT BỊ GIẤU KÍN', '⚠️ ĐỪNG MUA VỘI', '🔥 ẢO MA KHÔNG TIN NỔI').
   - Text overlay này đập thẳng vào mắt người lướt video khi chưa bật tiếng, đồng thời phá vỡ cấu trúc pixel hash của 5-10s đầu.
"""

EVIDENCE_AWARE_GUIDELINES = """
=== NGUYÊN TẮC BẢN QUYỀN & CHÂN THẬT HÌNH ẢNH (EVIDENCE-AWARE SCRIPTING) ===
1. CHỈ NÓI NHỮNG GÌ MẮT THẤY:
   - Khi quan sát video, chỉ mô tả những gì thực sự xuất hiện trong khung hình (màu sắc, hành động, bao bì, thao tác thực tế).
   - Phân biệt rõ giữa Sự thật thấy được (Visual Evidence) và Suy đoán (Inference).
2. TUYỆT ĐỐI KHÔNG BỊA ĐẶT THÔNG SỐ (ZERO HALLUCINATION):
   - CẤM tự bịa: dung lượng pin (5000mAh), chuẩn chống nước (IP68), giảm giá ảo (giảm 70%), chứng nhận FDA / y tế nếu trên video không có chữ hoặc bằng chứng xác thực.
3. KÊU GỌI HÀNH ĐỘNG (CTA) KHẢ THI:
   - Không hứa 'Comment để mình gửi file tài liệu' nếu không có hệ thống chatbot tự động gửi.
   - Ưu tiên CTA tự nhiên: Lưu lại (Save), Thảo luận quan điểm dưới bình luận (Comment opinion), hoặc Xem link trong hồ sơ (Bio).
"""

SCRIPT_PROFILES: Dict[str, Dict[str, Any]] = {
    "affiliate": {
        "name": "Affiliate / TikTok Shop / Review Sản Phẩm",
        "hook_strategy": "Nghịch lý hoặc trải nghiệm thực tế bất ngờ sau khi dùng thử",
        "body_structure": "Nỗi đau khó chịu -> Đập hộp/thử nghiệm -> Tính năng then chốt -> Điểm trừ nhẹ tăng uy tín -> Hướng dẫn chỗ mua",
        "tone": "Chân thành, khách quan, trải nghiệm thực tế",
        "pace": "Vừa phải, nhấn mạnh chi tiết cầm nắm",
        "evidence_rule": "Chỉ nói những gì mắt thấy trên video (chất liệu, màu sắc, cảm giác cầm, thao tác thực tế). Phân biệt rõ thấy trên video vs suy đoán.",
        "allowed_vocab": ["trên tay", "nhìn thực tế", "ưng nhất là", "điểm trừ nhẹ", "đáng đồng tiền", "dùng thử", "tiện cực"],
        "forbidden_claims": "CẤM tự bịa: dung lượng pin (5000mAh), công suất, giảm giá ảo (70%), chuẩn chống nước, bảo hành trọn đời, chứng nhận FDA.",
        "cta_style": "Kêu gọi xem giỏ hàng/bio: 'Bác nào ưng thì link góc trái/bio nha!', 'Bà nào dùng rồi cho xin review dưới cmt!'",
    },
    "review": {
        "name": "Review sản phẩm thực chiến",
        "hook_strategy": "Nghịch lý hoặc trải nghiệm thực tế bất ngờ sau khi dùng thử",
        "body_structure": "Nỗi đau khó chịu -> Đập hộp/thử nghiệm -> Tính năng then chốt -> Điểm trừ nhẹ tăng uy tín -> Hướng dẫn chỗ mua",
        "tone": "Chân thành, khách quan, trải nghiệm thực tế",
        "pace": "Vừa phải, nhấn mạnh chi tiết",
        "evidence_rule": "Chỉ nói những gì mắt thấy trên video (chất liệu, màu sắc, cảm giác cầm, thao tác thực tế). Không bịa thông số pin, chống nước hoặc chứng nhận y tế khi video không thể hiện.",
        "allowed_vocab": ["trên tay", "nhìn thực tế", "ưng nhất là", "điểm trừ nhẹ", "đáng đồng tiền"],
        "forbidden_claims": "Cấm bịa thông số pin, chống nước, chứng nhận y tế.",
        "cta_style": "Hỏi ý kiến hoặc hướng dẫn chỗ xem chi tiết: 'Bác nào đang dùng rồi cho mình xin cảm nhận nha!'",
    },
    "food": {
        "name": "Food Review / Ẩm thực",
        "hook_strategy": "Cận cảnh miếng cắn hoặc hương vị bùng nổ, giá cả gây sốc",
        "body_structure": "Cận cảnh món ăn -> Miêu tả độ giòn/béo/vị giác -> Không gian & giá cả thực tế -> Rủ bạn bè đi ăn",
        "tone": "Sôi nổi, kích thích vị giác, hào hứng",
        "pace": "Nhanh, tươi vui, cuốn hút",
        "evidence_rule": "Tập trung miêu tả vị giác, độ giòn, độ béo, khói bốc lên. Không phán xét vệ sinh an toàn khi không có chứng cứ.",
        "allowed_vocab": ["ngập topping", "chảy phô mai", "đậm vị", "giòn rụm", "cuốn lưỡi", "nóng hổi", "đẫm sốt"],
        "forbidden_claims": "Cấm phán xét vệ sinh an toàn thực phẩm không có bằng chứng, cấm cam kết 'ngon nhất Sài Gòn/Hà Nội'.",
        "cta_style": "Lưu quán hoặc tag bạn bè: 'Lưu lại liền cuối tuần rủ đứa bạn đi thẩm nha!'",
    },
    "tech": {
        "name": "Tech & Gadget",
        "hook_strategy": "Tính năng ẩn độc lạ giải quyết đúng 1 nỗi đau khó chịu hàng ngày",
        "body_structure": "Vấn đề công nghệ hàng ngày -> Thao tác mẹo/tính năng độc -> Trải nghiệm độ mượt -> Hỏi người xem",
        "tone": "Gãy gọn, logic, am hiểu công nghệ",
        "pace": "Nhanh, dứt khoát",
        "evidence_rule": "Chỉ review tính năng đang hiển thị trên màn hình/thiết bị. Cấm bịa thông số kỹ thuật (mAh, GHz, IP rating).",
        "allowed_vocab": ["mượt mà", "tiện cực kỳ", "thao tác một chạm", "đỡ tốn công", "ẩn giấu", "cực nhạy"],
        "forbidden_claims": "Cấm bịa thông số kỹ thuật (chipset, GHz, mAh, chuẩn IP, tốc độ sạc W).",
        "cta_style": "Hỏi trải nghiệm: 'Anh em thấy tính năng này có đáng tiền không?'",
    },
    "beauty": {
        "name": "Skincare & Beauty",
        "hook_strategy": "Sai lầm phổ biến khiến da không đẹp hoặc kết quả thay đổi rõ rệt",
        "body_structure": "Sai lầm chăm sóc da -> Texture/chất kem thực tế -> Cảm giác thẩm thấu -> Tip dùng đúng cách",
        "tone": "Nhẹ nhàng, chân thành, tâm tình",
        "pace": "Thư thả, dễ chịu",
        "evidence_rule": "Tập trung cảm giác texture, độ thấm, finish trên da. Cấm cam kết 'trị dứt điểm 100%' hay 'chuẩn y khoa' khi không có bằng chứng.",
        "allowed_vocab": ["thấm nhanh", "mỏng nhẹ", "không bết rít", "mướt mịn", "căng bóng", "ráo mịn"],
        "forbidden_claims": "CẤM cam kết 'trị dứt điểm 100%', 'khỏi mụn sau 3 ngày', 'thần dược chuẩn y khoa', 'trắng bật 3 tone ngay'.",
        "cta_style": "Chia sẻ tip: 'Mấy bà da dầu lưu lại tham khảo nha!'",
    },
    "explainer": {
        "name": "Kiến thức & Explainer",
        "hook_strategy": "Sự thật đằng sau một hiện tượng ai cũng thấy nhưng ít người hiểu",
        "body_structure": "Hiện tượng lạ ai cũng thấy -> Bản chất khoa học/kinh tế -> Ẩn dụ đời thường dễ hiểu -> Bài học sâu sắc",
        "tone": "Lôi cuốn, kích thích tò mò, mở mang tri thức",
        "pace": "Rõ ràng, nhấn nhá nhịp thở",
        "evidence_rule": "Giải thích bằng ngôn từ bình dân, so sánh ẩn dụ đời thường. Tránh thuật ngữ hàn lâm trừu tượng.",
        "allowed_vocab": ["thật ra thì", "nói dễ hiểu là", "tưởng vậy mà không phải vậy", "bí mật ở chỗ", "mấu chốt là"],
        "forbidden_claims": "Cấm đưa ra số liệu thống kê vô căn cứ, cấm khẳng định thuyết âm mưu chưa kiểm chứng.",
        "cta_style": "Kích thích bàn luận: 'Theo anh em thì giải pháp này có khả thi không?'",
    },
    "storytime": {
        "name": "Tâm sự / Storytime",
        "hook_strategy": "Tình huống éo le dở khóc dở cười hoặc bí mật chưa từng kể",
        "body_structure": "Mở đầu tình huống sốc -> Cao trào bất ngờ -> Cách ứng biến hài hước -> Bài học tâm đắc",
        "tone": "Hài hước, gần gũi, biểu cảm sống động",
        "pace": "Biến hóa theo diễn biến câu chuyện",
        "evidence_rule": "Lời kể chân thật, cảm xúc đời thực, không giả tạo hay dựng chuyện lừa đảo.",
        "allowed_vocab": ["hú hồn", "dở khóc dở cười", "nói không ai tin", "đúng là cái số", "cười ra nước mắt"],
        "forbidden_claims": "Cấm bịa chuyện bôi nhọ người khác hoặc gây hoang mang xã hội.",
        "cta_style": "Tương tác trải nghiệm: 'Có ai từng bị giống tui chưa, kể nghe với!'",
    }
}


PRONOUN_PERSONAS: Dict[str, Dict[str, Any]] = {
    "auto": {
        "label": "Tự động theo video / Giọng đọc",
        "description": "Tự động nhận diện nhân vật trong video hoặc căn cứ giọng đọc để chọn ngôi xưng chuẩn.",
        "rule": (
            "=== QUY TẮC ĐẠI TỪ NHÂN XƯNG & GIỚI TÍNH (BẮT BUỘC NHẤT QUÁN 100%): ===\n"
            "1. HÃY QUAN SÁT NHÂN VẬT CHÍNH XUẤT HIỆN TRONG CÁC KHUNG HÌNH VIDEO HOẶC GIỌNG ĐỌC:\n"
            "   - Nếu nhân vật chính hoặc người dẫn chuyện là CON TRAI / NAM GIỚI:\n"
            "     * BẮT BUỘC xưng 'anh' (hoặc 'mình'), gọi người xem là 'em', 'các bạn', 'mọi người', 'mấy đứa'.\n"
            "     * TUYỆT ĐỐI CẤM người nói tự xưng là 'em' hoặc 'chị'!\n"
            "   - Nếu nhân vật chính hoặc người dẫn chuyện là CON GÁI / NỮ GIỚI:\n"
            "     * BẮT BUỘC xưng 'em' (hoặc 'mình', 'tui'), gọi người xem là 'anh/chị', 'mọi người', 'mấy bà', 'các bác'.\n"
            "     * TUYỆT ĐỐI CẤM người nói tự xưng là 'anh'!\n"
            "   - Nếu video review đồ vật/cảnh quan không thấy rõ mặt: Ưu tiên xưng 'mình' - gọi 'bạn'/'mọi người', hoặc đối chiếu với giới tính giọng đọc (nếu có).\n"
            "2. BẢO TOÀN NGÔI XƯNG XUYÊN SUỐT: Toàn bộ kịch bản từ Cảnh 1 (Hook) đến Cảnh cuối (CTA) PHẢI DÙNG ĐỒNG NHẤT MỘT NGÔI XƯNG. Tuyệt đối cấm cảnh này xưng 'anh', cảnh sau lại nhảy sang xưng 'em'."
        )
    },
    "male_anh": {
        "label": "Nam xưng Anh (gọi Em / Bạn)",
        "description": "Người nói là nam giới, xưng 'anh', gọi 'em', 'mọi người', 'các bạn'.",
        "rule": (
            "=== QUY TẮC XƯNG HÔ BẮT BUỘC: NAM XƯNG ANH ===\n"
            "- Người nói là NAM GIỚI. Toàn bộ kịch bản BẮT BUỘC tự xưng là 'anh' (hoặc 'mình'), gọi người xem là 'em' / 'mấy đứa' / 'các bạn' / 'mọi người'.\n"
            "- TUYỆT ĐỐI CẤM TỰ XƯNG LÀ 'EM' HAY 'CHỊ' trong bất kỳ phân cảnh nào!\n"
            "- Đảm bảo đồng nhất 100% ngôi xưng 'anh' từ Hook đến CTA."
        )
    },
    "female_em": {
        "label": "Nữ xưng Em (gọi Anh Chị / Mọi người)",
        "description": "Người nói là nữ giới, xưng 'em', gọi 'anh/chị', 'các bác', 'mọi người'.",
        "rule": (
            "=== QUY TẮC XƯNG HÔ BẮT BUỘC: NỮ XƯNG EM ===\n"
            "- Người nói là NỮ GIỚI. Toàn bộ kịch bản BẮT BUỘC tự xưng là 'em' (hoặc 'mình'), gọi người xem là 'anh/chị' / 'mọi người' / 'các bác' / 'mấy bà'.\n"
            "- TUYỆT ĐỐI CẤM TỰ XƯNG LÀ 'ANH' trong bất kỳ phân cảnh nào!\n"
            "- Đảm bảo đồng nhất 100% ngôi xưng 'em' từ Hook đến CTA."
        )
    },
    "friendly_minh": {
        "label": "Xưng Mình - Gọi Bạn (Thân thiện trung tính)",
        "description": "Xưng 'mình', gọi 'bạn' hoặc 'mọi người', phù hợp mọi lứa tuổi và giới tính.",
        "rule": (
            "=== QUY TẮC XƯNG HÔ BẮT BUỘC: XƯNG MÌNH - GỌI BẠN ===\n"
            "- Người nói tự xưng là 'mình', gọi người xem là 'bạn' / 'mọi người' / 'cả nhà'.\n"
            "- Giữ vị thế trung tính, bình đẳng và thân thiện. Không xưng 'anh', không xưng 'em'."
        )
    },
    "genz_tui": {
        "label": "Xưng Tui - Gọi Mấy bà / Mấy ní (Dí dỏm TikTok)",
        "description": "Xưng 'tui', gọi 'mấy bà', 'mấy ní', phong cách trẻ trung hài hước.",
        "rule": (
            "=== QUY TẮC XƯNG HÔ BẮT BUỘC: XƯNG TUI - GỌI MẤY BÀ / MẤY NÍ ===\n"
            "- Người nói tự xưng là 'tui', gọi người xem là 'mấy bà' / 'mấy ní' / 'anh chị em'.\n"
            "- Giọng điệu hài hước, dí dỏm, phong cách Gen Z TikTok."
        )
    }
}


def sanitize_script_pronouns(
    scenes: List[Any],
    persona: str = "auto",
    speaker_pronoun: Optional[str] = None
) -> List[Any]:
    """
    Tự động kiểm tra và chuẩn hóa đại từ nhân xưng tránh lỗi nhầm lẫn (ví dụ con trai xưng em hoặc ngược lại).
    Đảm bảo tính nhất quán tuyệt đối giữa các phân cảnh.
    """
    p_mode = (persona or "auto").lower().strip()
    pronoun = (speaker_pronoun or "").lower().strip()

    is_male = p_mode == "male_anh" or (p_mode == "auto" and pronoun in ("anh", "male"))
    is_female = p_mode == "female_em" or (p_mode == "auto" and pronoun in ("em", "female"))

    for sc in scenes:
        is_obj = hasattr(sc, "speaker_text")
        text = (sc.speaker_text if is_obj else sc.get("speaker_text")) or ""

        if is_male:
            # Nam xưng Anh: Sửa các lỗi AI vô tình xưng "em"
            text = re.sub(r'\b[Ee]m (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn|chào)\b', r'Anh \1', text)
            text = re.sub(r'\b[Cc]ủa em\b', 'của anh', text)
        elif is_female:
            # Nữ xưng Em: Sửa các lỗi AI vô tình xưng "anh"
            text = re.sub(r'\b[Aa]nh (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn|chào)\b', r'Em \1', text)
            text = re.sub(r'\b[Cc]ủa anh\b', 'của em', text)
        elif p_mode == "friendly_minh":
            text = re.sub(r'\b[Aa]nh (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn)\b', r'Mình \1', text)
            text = re.sub(r'\b[Ee]m (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn)\b', r'Mình \1', text)
        elif p_mode == "genz_tui":
            text = re.sub(r'\b[Aa]nh (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn)\b', r'Tui \1', text)
            text = re.sub(r'\b[Ee]m (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn)\b', r'Tui \1', text)
            text = re.sub(r'\b[Mm]ình (thấy|nghĩ|xin|khuyên|vừa|test|thử|mua|dùng|xài|mách|chia sẻ|hướng dẫn)\b', r'Tui \1', text)

        w_cnt = len(text.split())
        if is_obj:
            sc.speaker_text = text
            sc.word_count = w_cnt
        else:
            sc["speaker_text"] = text
            sc["word_count"] = w_cnt

    return scenes


def generate_viral_hooks(
    topic: str,
    api_key: Optional[str] = None,
    num_hooks: int = 6,
    hook_duration: int = 7,
    mode: str = "anti_copyright"
) -> List[Dict[str, Any]]:
    """Tạo các biến thể hook 5-10s theo công thức Hook Engine v2 chuyên dụng để né bản quyền & thu hút người xem."""
    target_words = int(round(hook_duration * 2.4))
    min_words = max(10, target_words - 3)
    max_words = target_words + 3

    system_instruction = (
        "Bạn là một bậc thầy sáng tạo nội dung video ngắn hàng đầu cho TikTok, Reels và YouTube Shorts. "
        f"Nhiệm vụ của bạn là tạo các câu Hook {hook_duration} giây đầu (khoảng {min_words}-{max_words} từ) chuyên dụng để: "
        "1. NÉ QUÉT BẢN QUYỀN: Phủ kín 100% âm thanh mở đầu bằng lời thoại dẫn chuyện/bình luận mới toanh của creator, bẻ lái ngữ cảnh video (Re-framing). "
        "2. THU HÚT & GIỮ CHÂN: Dừng ngón tay lướt trong 3s đầu và mở vòng lặp tò mò (curiosity loop) buộc người xem phải xem tiếp. "
        "Tuyệt đối KHÔNG mở đầu bằng 'Chào các bạn', 'Hôm nay mình sẽ...', 'Tôi là...'. "
        "Lời văn phải 100% tự nhiên như người thật nói chuyện ngoài đời, biểu cảm sống động, không máy móc. "
        + HUMAN_VOICE_GUIDELINES
        + "\n\n"
        + ANTI_COPYRIGHT_HOOK_GUIDELINES
    )

    prompt = f"""Hãy tạo {num_hooks} câu Hook khác nhau cho chủ đề sau:
Chủ đề: "{topic}"
Thời lượng Hook mục tiêu: khoảng {hook_duration} giây (khoảng {min_words} đến {max_words} từ).

Yêu cầu xuất ra đúng định dạng mảng JSON thuần túy (không markdown thừa):
[
  {{
    "type": "Bẻ lái ngữ cảnh / Vạch trần sự thật",
    "angle": "reframe_truth",
    "hook": "Khoan lướt qua! Đừng để đoạn mở đầu này đánh lừa bạn, sự thật đằng sau cảnh này sẽ khiến bạn ngã ngửa đấy...",
    "overlay_text": "🚨 SỰ THẬT BỊ GIẤU KÍN",
    "duration_seconds": {hook_duration},
    "visual_match": 0.92,
    "curiosity": 0.95,
    "credibility": 0.85,
    "specificity": 0.88
  }},
  ...
]

6 Loại Hook cụ thể (tương ứng với angle):
1. Bẻ lái ngữ cảnh / Vạch trần sự thật (angle: "reframe_truth")
2. Cảnh báo khẩn cấp / Sai lầm mất tiền (angle: "shock_warning")
3. Bình luận / Phản ứng độc quyền (angle: "reaction_commentary")
4. Bẫy tò mò giữ chân đến cuối (angle: "curiosity_loop")
5. Nghịch lý đảo ngược 180 độ (angle: "contrarian_twist")
6. Mẹo nhanh / Giá trị tức thì (angle: "speed_hack")

Chỉ trả về JSON hợp lệ."""

    def _get_default_anti_copyright_hooks():
        return [
            {
                "type": "Bẻ lái ngữ cảnh / Vạch trần sự thật",
                "angle": "reframe_truth",
                "hook": f"Khoan lướt qua! Đừng để đoạn mở đầu này đánh lừa bạn, sự thật đằng sau {topic} này sẽ khiến bạn ngã ngửa đấy!",
                "overlay_text": "🚨 SỰ THẬT BỊ GIẤU KÍN",
                "duration_seconds": float(hook_duration),
                "visual_match": 0.88,
                "visual_potential": 0.90,
                "curiosity": 0.95,
                "credibility": 0.85,
                "specificity": 0.88
            },
            {
                "type": "Cảnh báo khẩn cấp / Sai lầm mất tiền",
                "angle": "shock_warning",
                "hook": f"Dừng ngay nếu không muốn mất tiền oan! {topic} đang bị cảnh báo rầm rộ mà 90% người dùng không hề hay biết.",
                "overlay_text": "⚠️ CẢNH BÁO: DỪNG LẠI NGAY!",
                "duration_seconds": float(hook_duration),
                "visual_match": 0.85,
                "visual_potential": 0.88,
                "curiosity": 0.92,
                "credibility": 0.90,
                "specificity": 0.85
            },
            {
                "type": "Bình luận / Phản ứng độc quyền",
                "angle": "reaction_commentary",
                "hook": f"Thề luôn xem tới cảnh này mình không tin nổi vào mắt mình! Để mình giải thích cho bạn xem tại sao lại như vậy nha.",
                "overlay_text": "😱 PHẢN ỨNG BẤT NGỜ",
                "duration_seconds": float(hook_duration),
                "visual_match": 0.90,
                "visual_potential": 0.85,
                "curiosity": 0.90,
                "credibility": 0.85,
                "specificity": 0.80
            },
            {
                "type": "Bẫy tò mò giữ chân đến cuối",
                "angle": "curiosity_loop",
                "hook": f"Đừng rời mắt khỏi giây thứ 5 của video này, chi tiết bất ngờ nhất về {topic} sẽ khiến bạn phải xem đi xem lại!",
                "overlay_text": "🔥 ĐỪNG RỜI MẮT!",
                "duration_seconds": float(hook_duration),
                "visual_match": 0.92,
                "visual_potential": 0.92,
                "curiosity": 0.96,
                "credibility": 0.80,
                "specificity": 0.85
            },
            {
                "type": "Nghịch lý đảo ngược 180 độ",
                "angle": "contrarian_twist",
                "hook": f"Ai cũng nghĩ {topic} là chân ái nhưng sau khi thử thực tế thì đây là kết quả hoàn toàn trái ngược.",
                "overlay_text": "🔄 ĐẢO NGƯỢC NGHỊCH LÝ",
                "duration_seconds": float(hook_duration),
                "visual_match": 0.85,
                "visual_potential": 0.85,
                "curiosity": 0.88,
                "credibility": 0.85,
                "specificity": 0.82
            },
            {
                "type": "Mẹo nhanh / Giá trị tức thì",
                "angle": "speed_hack",
                "hook": f"Lưu lại liền 3 mẹo cực đỉnh về {topic} này, xem đúng 7 giây đầu là bạn tiết kiệm được cả triệu đồng rồi!",
                "overlay_text": "💡 MẸO ĐỈNH LƯU NGAY",
                "duration_seconds": float(hook_duration),
                "visual_match": 0.86,
                "visual_potential": 0.88,
                "curiosity": 0.90,
                "credibility": 0.90,
                "specificity": 0.90
            }
        ]

    raw_text = call_gemini(
        prompt,
        api_key=api_key,
        system_instruction=system_instruction,
        response_schema=HOOKS_SCHEMA
    )
    if not raw_text:
        return _get_default_anti_copyright_hooks()

    match = re.search(r'\[.*\]', raw_text, re.DOTALL)
    if match:
        raw_text = match.group(0)

    try:
        data = json.loads(raw_text)
        # Normalize and ensure metadata exists
        hooks_out = []
        for h in data:
            if isinstance(h, dict) and h.get("hook"):
                v_match = float(h.get("visual_match") or h.get("visual_potential") or 0.85)
                v_pot = float(h.get("visual_potential") or h.get("visual_match") or 0.85)
                hooks_out.append({
                    "type": str(h.get("type") or "Sáng tạo").strip(),
                    "angle": str(h.get("angle") or "curiosity_loop").strip(),
                    "hook": str(h.get("hook")).strip(),
                    "overlay_text": str(h.get("overlay_text") or "").strip(),
                    "duration_seconds": float(h.get("duration_seconds") or hook_duration),
                    "visual_match": v_match,
                    "visual_potential": v_pot,
                    "curiosity": float(h.get("curiosity") or 0.85),
                    "credibility": float(h.get("credibility") or 0.80),
                    "specificity": float(h.get("specificity") or 0.80)
                })
        return hooks_out or _get_default_anti_copyright_hooks()
    except Exception as e:
        logger.error(f"Lỗi parse JSON hooks: {e}, text: {raw_text}")
        return _get_default_anti_copyright_hooks()


def generate_video_script(
    topic: str,
    platform: str = "tiktok",
    duration_target: int = 45,
    style: str = "engaging",
    hook_text: Optional[str] = None,
    custom_instruction: str = "",
    profile: str = "review",
    persona: str = "auto",
    voice: Optional[str] = None,
    hook_duration: Optional[float] = 7.0,
    anti_copyright: bool = True,
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Sinh kịch bản video ngắn hoàn chỉnh kế thừa kỹ năng reels-scripting từ social-media-skills.
    Timeline được tính toán động (dynamic timeline) khớp chính xác với duration_target, không hard-code.
    Tích hợp runtime Script Profiles, Evidence-aware prompting, Pronoun Persona, Hook 5-10s né bản quyền và kiểm duyệt Retention Risk.
    """
    # Lấy thông tin profile tương ứng
    prof_key = (profile or "review").lower().strip()
    if prof_key not in SCRIPT_PROFILES:
        prof_key = "affiliate" if prof_key == "review" else "affiliate"
    prof_data = SCRIPT_PROFILES.get(prof_key, SCRIPT_PROFILES.get("affiliate", {}))

    # Xử lý quy tắc đại từ nhân xưng (Pronoun Persona)
    persona_key = (persona or "auto").lower().strip()
    persona_info = PRONOUN_PERSONAS.get(persona_key, PRONOUN_PERSONAS["auto"])
    persona_rule = persona_info["rule"]
    if voice:
        v_lower = str(voice).lower()
        if any(k in v_lower for k in ["075", "felipe", "male", "nam", "thanh niên"]):
            persona_rule += "\n[LƯU Ý GIỌNG ĐỌC]: Đang dùng giọng nam. BẮT BUỘC xưng 'anh' (hoặc 'mình'), TUYỆT ĐỐI KHÔNG xưng 'em'!"
        elif any(k in v_lower for k in ["562", "421", "female", "mai", "nữ", "nhỏ ngọt ngào"]):
            persona_rule += "\n[LƯU Ý GIỌNG ĐỌC]: Đang dùng giọng nữ. BẮT BUỘC xưng 'em' (hoặc 'mình'), TUYỆT ĐỐI KHÔNG xưng 'anh'!"

    profile_guide = f"""
=== CHIẾN LƯỢC NỘI DUNG THEO PROFILE: {prof_data.get('name')} ===
- Hook Strategy: {prof_data.get('hook_strategy')}
- Cấu trúc thân bài: {prof_data.get('body_structure')}
- Tone & Pace: {prof_data.get('tone')}, nhịp {prof_data.get('pace')}
- Quy tắc bằng chứng (Evidence): {prof_data.get('evidence_rule')}
- Từ ngữ gợi ý dùng: {', '.join(prof_data.get('allowed_vocab', []))}
- TUYỆT ĐỐI CẤM: {prof_data.get('forbidden_claims')}
- Định hướng CTA: {prof_data.get('cta_style')}
"""

    system_instruction = (
        "Bạn là giám đốc sáng tạo nội dung video ngắn hàng đầu cho TikTok và Reels. "
        "Bạn luôn viết kịch bản bằng VĂN PHONG NGƯỜI THẬT, gần gũi, dí dỏm và chân thành. "
        "Tuyệt đối không dùng văn mẫu AI khô cứng, không dùng từ sáo rỗng. "
        + HUMAN_VOICE_GUIDELINES
        + "\n\n"
        + (ANTI_COPYRIGHT_HOOK_GUIDELINES if anti_copyright else "")
        + "\n\n"
        + persona_rule
        + "\n\n"
        + EVIDENCE_AWARE_GUIDELINES
    )

    timeline = build_scene_timeline(
        duration_target,
        hook_duration=hook_duration if anti_copyright else None
    )

    if hook_text:
        hook_prompt = f'Bắt buộc dùng câu Hook này làm mở đầu: "{hook_text}"'
    else:
        h_dur_val = round(timeline[0]["end_seconds"] - timeline[0]["start_seconds"], 1)
        hook_prompt = f"Bắt buộc sáng tạo một Hook né bản quyền kéo dài đúng {h_dur_val} giây đầu: bẻ lái ngữ cảnh video, ngắt trạng thái lướt trong 3s đầu và mở vòng lặp tò mò giữ chân người xem."

    scene_prompt_items = []
    for sc in timeline:
        sec = sc["section"]
        idx = sc["index"]
        s_sec = sc["start_seconds"]
        e_sec = sc["end_seconds"]
        t_range = sc["time_range"]
        target_words = int(round((e_sec - s_sec) * 2.4))

        if sec == "hook":
            h_dur = round(e_sec - s_sec, 1)
            desc = f"Mở đầu {h_dur}s né bản quyền & giữ chân cực đỉnh (khoảng {target_words} từ). Bẻ lái ngữ cảnh, mở vòng lặp tò mò, đọc liên tục không ngắt quãng..."
            v_cue = "Cận cảnh hành động giật gân hoặc biểu cảm bất ngờ..."
            overlay = "TIÊU ĐỀ HOOK BẺ LÁI GÂY SỐC"
        elif "point" in sec:
            desc = f"Lời thoại phần {sec} bám sát cấu trúc {prof_data.get('body_structure')} (khoảng {target_words} từ, nói tự nhiên, nhịp câu ngắn)..."
            v_cue = f"Cảnh quay minh họa nội dung {sec}..."
            overlay = f"TỪ KHÓA ĐIỂM {idx}"
        else:
            desc = f"Lời kêu gọi hành động chuẩn {prof_data.get('cta_style')} (khoảng {target_words} từ, tự nhiên, chân thật)..."
            v_cue = "Chỉ tay hoặc biểu cảm thân thiện..."
            overlay = "KÊU GỌI HÀNH ĐỘNG"

        scene_prompt_items.append(f"""    {{
      "index": {idx},
      "section": "{sec}",
      "start_seconds": {s_sec},
      "end_seconds": {e_sec},
      "time_range": "{t_range}",
      "speaker_text": "{desc}",
      "visual_cue": "{v_cue}",
      "text_overlay": "{overlay}"
    }}""")

    scenes_json_template = "[\n" + ",\n".join(scene_prompt_items) + "\n  ]"

    prompt = f"""Hãy viết một kịch bản video ngắn hoàn chỉnh cho chủ đề sau:
- Chủ đề: "{topic}"
- Nền tảng đích: {platform} (TikTok / Instagram Reels / Shorts)
- Thời lượng mục tiêu: khoảng {duration_target} giây (tổng số từ khoảng {int(duration_target * 2.4)} từ)
- Phong cách: {style}
- {hook_prompt}
{f"- Ghi chú bổ sung: {custom_instruction}" if custom_instruction else ""}

{profile_guide}

{HUMAN_VOICE_GUIDELINES}

BẮT BUỘC trả về đúng định dạng JSON tuân thủ timeline {len(timeline)} phân cảnh sau:
{{
  "title": "Tiêu đề video cuốn hút",
  "topic": "{topic}",
  "profile": "{prof_key}",
  "estimated_total_seconds": {duration_target},
  "scenes": {scenes_json_template}
}}

Lưu ý: Viết đúng {len(timeline)} phân cảnh theo đúng mốc thời gian start_seconds và end_seconds đã phân bổ ở trên sao cho tổng thời lượng đúng xấp xỉ {duration_target} giây.
Chỉ trả về JSON thuần túy, không có text markdown mở đầu hay kết thúc."""

    raw_text = call_gemini(
        prompt,
        api_key=api_key,
        system_instruction=system_instruction,
        response_schema=GENERATED_SCRIPT_SCHEMA
    )
    if not raw_text:
        raise RuntimeError("Gemini không trả về kết quả kịch bản.")

    try:
        raw_json = parse_gemini_json(raw_text)
        validated = validate_and_normalize_script(raw_json, video_duration=duration_target)
        validated["profile"] = prof_key
        validated["persona"] = persona_key
        validated["scenes"] = sanitize_script_pronouns(
            validated["scenes"],
            persona=persona_key,
            speaker_pronoun=validated.get("speaker_pronoun")
        )
        # Runtime Retention QA & 1-pass targeted repair
        risks = analyze_script_retention_risk(validated["scenes"], video_duration=duration_target)
        validated["retention_risks"] = risks
        high_risks = [r for r in risks if r.get("risk_level") == "HIGH"]
        if high_risks:
            target_idx = high_risks[0]["scene"] - 1
            if 0 <= target_idx < len(validated["scenes"]):
                sc = validated["scenes"][target_idx]
                target_d = sc.get("target_duration") or sc.get("duration_seconds") or 3.0
                repaired = rewrite_scene_for_duration(
                    scene=sc,
                    target_duration=target_d,
                    actual_duration=target_d + 1.2,
                    api_key=api_key,
                    profile=prof_key
                )
                validated["scenes"][target_idx] = repaired
                validated = validate_and_normalize_script(validated, video_duration=duration_target)
                validated["retention_risks"] = analyze_script_retention_risk(validated["scenes"], video_duration=duration_target)
        return validated
    except Exception as e:
        logger.error(f"Lỗi validate kịch bản: {e}, text: {raw_text}")
        raise RuntimeError(f"Không thể phân tích dữ liệu kịch bản từ AI: {e}")


def extract_smart_video_frames(
    video_path: str | Path,
    max_frames: int = 8,
    target_width: int = 640
) -> Tuple[List[Dict[str, Any]], float, Dict[str, Any]]:
    """
    Trích xuất các khung hình đại diện theo dòng thời gian của video để AI Vision phân tích.
    Tự động đo độ dài, FPS, kích thước và nén ảnh JPEG base64 tối ưu băng thông.
    """
    clean_path = clean_fs_path(video_path)
    if not clean_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file video: {clean_path}")

    cap = cv2.VideoCapture(str(clean_path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Không thể mở video qua OpenCV: {clean_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_seconds = total_frames / fps if fps > 0 else 0.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if duration_seconds <= 0:
            raise ValueError("Không xác định được thời lượng video hoặc video bị lỗi.")

        sample_times = calculate_sample_timestamps(duration_seconds, max_frames=max_frames)

        frames_data = []
        for ts in sample_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
            ret, frame = cap.read()
            if ret and frame is not None:
                h, w = frame.shape[:2]
                scale = float(target_width) / max(w, h)
                if scale < 1.0:
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
                
                _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                b64_str = base64.b64encode(buffer).decode('utf-8')
                mm = int(ts // 60)
                ss = int(ts % 60)
                frames_data.append({
                    "timestamp_sec": ts,
                    "timestamp_str": f"{mm:02d}:{ss:02d}",
                    "b64": b64_str
                })
    finally:
        cap.release()
    metadata = {
        "duration_seconds": round(duration_seconds, 2),
        "fps": round(fps, 2),
        "width": width,
        "height": height,
        "sampled_frames": len(frames_data)
    }
    return frames_data, duration_seconds, metadata


def analyze_video_and_generate_script(
    video_path: str | Path,
    genre: str = "review",
    style: str = "engaging",
    custom_instruction: str = "",
    profile: Optional[str] = None,
    persona: str = "auto",
    voice: Optional[str] = None,
    hook_duration: Optional[float] = 7.0,
    anti_copyright: bool = True,
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    AI xem video qua các khung hình thời gian thực (Gemini Vision),
    hiểu nội dung đang diễn ra (sản phẩm gì, hành động gì) và
    dựng kịch bản chuẩn viral khớp chính xác với thời lượng video gốc.
    Tích hợp runtime Script Profiles, Evidence-aware prompting, Pronoun Persona, Hook 5-10s né bản quyền và kiểm duyệt Retention Risk.
    """
    frames_data, duration_seconds, metadata = extract_smart_video_frames(video_path, max_frames=8)
    
    target_duration = int(round(duration_seconds))
    target_words = int(round(duration_seconds * 2.4))
    
    prof_key = (profile or genre or "review").lower().strip()
    if prof_key not in SCRIPT_PROFILES:
        prof_key = "affiliate" if prof_key in ("review", "auto") else "explainer" if prof_key == "documentary" else "storytime" if prof_key == "story" else "affiliate"
    prof_data = SCRIPT_PROFILES.get(prof_key, SCRIPT_PROFILES.get("affiliate", {}))

    # Xử lý quy tắc đại từ nhân xưng (Pronoun Persona)
    persona_key = (persona or "auto").lower().strip()
    persona_info = PRONOUN_PERSONAS.get(persona_key, PRONOUN_PERSONAS["auto"])
    persona_rule = persona_info["rule"]
    if voice:
        v_lower = str(voice).lower()
        if any(k in v_lower for k in ["075", "felipe", "male", "nam", "thanh niên"]):
            persona_rule += "\n[LƯU Ý GIỌNG ĐỌC]: Giọng đọc TTS đã chọn là GIỌNG NAM (Nam Trầm / Thanh Niên). BẮT BUỘC xưng 'anh' (hoặc 'mình'), TUYỆT ĐỐI KHÔNG xưng 'em'!"
        elif any(k in v_lower for k in ["562", "421", "female", "mai", "nữ", "nhỏ ngọt ngào"]):
            persona_rule += "\n[LƯU Ý GIỌNG ĐỌC]: Giọng đọc TTS đã chọn là GIỌNG NỮ (Mai / Nhỏ Ngọt Ngào). BẮT BUỘC xưng 'em' (hoặc 'mình'), TUYỆT ĐỐI KHÔNG xưng 'anh'!"

    genre_guidelines = {
        "review": (
            "Thể loại: REVIEW SẢN PHẨM NGOÀI ĐỜI THỰC. "
            "Cách nói chuyện: Giống như bạn vừa mua được món đồ này về dùng thử và quá ưng ý nên quay video chia sẻ thật lòng với bạn bè. "
            "Quan sát kỹ từng khung hình: Nhìn rõ món đồ là gì, kiểu dáng, màu sắc, cách cầm nắm hay thao tác sử dụng thực tế. "
            "Lời thoại phải trầm trồ, chân thật, khen điểm mạnh nhất và chỉ ra sự tiện lợi thực tế, không tâng bốc sách vở."
        ),
        "documentary": (
            "Thể loại: THUYẾT MINH / KỂ CHUYỆN CUỐN HÚT. "
            "Cách nói chuyện: Giọng điệu của một người kể chuyện cuốn hút, mở đầu bằng sự tò mò ('Ủa có bao giờ bạn thắc mắc...', 'Nhiều người tưởng là... nhưng thật ra...'). "
            "Dẫn dắt tự nhiên, giải thích đúng từng chi tiết xuất hiện trên màn hình, không giảng bài khô khan."
        ),
        "story": (
            "Thể loại: TÂM SỰ / ĐỜI SỐNG GẦN GŨI. "
            "Cách nói chuyện: Nhẹ nhàng, chân thành, như đang chia sẻ câu chuyện trải nghiệm của chính mình."
        ),
        "auto": (
            "Thể loại: TỰ ĐỘNG NHẬN DIỆN VÀ NÓI CHUYỆN TỰ NHIÊN. "
            "Tự nhận diện bối cảnh video và nói chuyện hoàn toàn bằng giọng điệu con người ngoài đời, sống động, chân thật."
        )
    }
    selected_genre_desc = genre_guidelines.get(genre, genre_guidelines["review"])

    profile_guide = f"""
=== CHIẾN LƯỢC NỘI DUNG THEO PROFILE: {prof_data.get('name')} ===
- Hook Strategy: {prof_data.get('hook_strategy')}
- Cấu trúc thân bài: {prof_data.get('body_structure')}
- Tone & Pace: {prof_data.get('tone')}, nhịp {prof_data.get('pace')}
- Quy tắc bằng chứng (Evidence): {prof_data.get('evidence_rule')}
- Từ ngữ gợi ý dùng: {', '.join(prof_data.get('allowed_vocab', []))}
- TUYỆT ĐỐI CẤM: {prof_data.get('forbidden_claims')}
- Định hướng CTA: {prof_data.get('cta_style')}
"""

    system_instruction = (
        "Bạn là một đạo diễn và biên kịch video ngắn hàng đầu cho TikTok, Reels và YouTube Shorts. "
        "Bạn có năng lực đọc hiểu thị giác (Visual Understanding) xuất sắc. "
        "Bạn luôn viết kịch bản bằng VĂN PHONG NGƯỜI THẬT NGOÀI ĐỜI: gần gũi, dí dỏm, tự nhiên, không sách vở hay máy móc. "
        "Kịch bản đọc thoại PHẢI KHỚP CHÍNH XÁC VỚI THỜI LƯỢNG VIDEO. "
        + HUMAN_VOICE_GUIDELINES
        + "\n\n"
        + (ANTI_COPYRIGHT_HOOK_GUIDELINES if anti_copyright else "")
        + "\n\n"
        + persona_rule
        + "\n\n"
        + EVIDENCE_AWARE_GUIDELINES
    )

    # Tạo timeline phân cảnh động thích ứng đúng thời lượng video và hook_duration
    suggested_timeline = build_scene_timeline(
        duration_seconds,
        hook_duration=hook_duration if anti_copyright else None
    )
    actual_hook_len = round(suggested_timeline[0]["end_seconds"] - suggested_timeline[0]["start_seconds"], 1) if suggested_timeline else 7.0
    hook_target_words = int(round(actual_hook_len * 2.4))

    parts: List[Dict[str, Any]] = []
    
    prompt_header = f"""Tôi cung cấp cho bạn {len(frames_data)} khung hình được trích xuất theo dòng thời gian từ một video thực tế dài {round(duration_seconds, 1)} giây ({target_duration}s).

{selected_genre_desc}
{profile_guide}
Phong cách yêu cầu: {style}.
{f"Yêu cầu bổ sung: {custom_instruction}" if custom_instruction else ""}

{HUMAN_VOICE_GUIDELINES}

YÊU CẦU TỐI QUAN TRỌNG VỀ THỜI LƯỢNG & KHỚP HÌNH:
1. Tổng thời lượng kịch bản PHẢI KHỚP VỚI VIDEO: khoảng {target_duration} giây.
2. Nhịp đọc tiếng Việt tự nhiên là ~2.3 đến 2.5 từ/giây. Do đó tổng số từ của toàn bộ lời thoại PHẢI XẤP XỈ: {target_words} từ.
3. Chia thành 3 đến 5 phân cảnh (scenes).
4. Mỗi phân cảnh có mốc thời gian (start_seconds -> end_seconds). Lời thoại của từng phân cảnh PHẢI MÔ TẢ VÀ DẪN DẮT ĐÚNG NHỮNG GÌ ĐANG XUẤT HIỆN TRÊN MÀN HÌNH ở khoảng thời gian đó.
5. Cấu trúc chuẩn viral & né quét bản quyền:
   - Scene 1 (0.0s đến {actual_hook_len}s): HOOK NÉ BẢN QUYỀN VÀ GIỮ CHÂN. Lời thoại kéo dài đúng {actual_hook_len} giây đầu (khoảng {hook_target_words} từ). BẮT BUỘC BẺ LÁI NGỮ CẢNH (Re-framing): Không thuật lại đơn điệu mà bình luận độc quyền hoặc cảnh báo/gây tò mò để che phủ 100% âm thanh gốc, biến video thành nội dung Fair Use mới toanh!
   - Text Overlay của Scene 1 phải là chữ TO IN HOA, ngắn gọn (dưới 6 từ) đập vào mắt người xem.
   - Các scene thân bài: Trực tiếp bám sát diễn biến hình ảnh (review sản phẩm hoặc thuyết minh).
   - Scene kết: Kêu gọi hành động (CTA) ngắn gọn, tự nhiên.

Dưới đây là các khung hình video theo từng mốc thời gian:"""

    parts.append({"text": prompt_header})

    for f in frames_data:
        parts.append({"text": f"\n[Khung hình tại thời điểm {f['timestamp_str']} - giây thứ {f['timestamp_sec']}s]:"})
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": f["b64"]
            }
        })

    timeline_sample_json = json.dumps([
        {
            "index": sc["index"],
            "section": sc["section"],
            "time_range": sc["time_range"],
            "planned_start": sc["planned_start"],
            "planned_end": sc["planned_end"],
            "start_seconds": sc["start_seconds"],
            "end_seconds": sc["end_seconds"],
            "speaker_text": f"Lời thoại ngắn gọn khớp cảnh #{sc['index']}...",
            "visual_cue": "Mô tả hình ảnh quan sát được từ video",
            "text_overlay": "TỪ KHÓA NỔI BẬT"
        }
        for sc in suggested_timeline
    ], ensure_ascii=False, indent=2)

    prompt_footer = f"""\nBẮT BUỘC trả về đúng định dạng JSON như sau (không có markdown thừa):
{{
  "title": "Tiêu đề video cuốn hút",
  "detected_subject": "Tên sản phẩm hoặc chủ đề cụ thể AI nhận diện được trong các khung hình",
  "speaker_gender": "male hoặc female hoặc unknown",
  "speaker_pronoun": "anh hoặc em hoặc minh hoặc tui",
  "genre": "{genre}",
  "profile": "{prof_key}",
  "video_duration": {round(duration_seconds, 1)},
  "summary": "Tóm tắt ngắn gọn 1-2 câu về nội dung video",
  "caption": "Caption 2 dòng giật tít để đăng video lên mạng xã hội",
  "hashtags": ["#tag1", "#tag2", "#tag3"],
  "scenes": {timeline_sample_json}
}}

Lưu ý: Các mốc start_seconds và end_seconds của từng cảnh PHẢI LIÊN TỤC và cảnh cuối cùng PHẢI KẾT THÚC ĐÚNG {round(duration_seconds, 1)}s.
Chỉ trả về JSON thuần túy."""

    parts.append({"text": prompt_footer})

    raw_text = call_gemini(
        parts=parts,
        api_key=api_key,
        system_instruction=system_instruction,
        response_schema=GENERATED_SCRIPT_SCHEMA
    )
    if not raw_text:
        raise RuntimeError("Gemini Vision không trả về kết quả phân tích video.")

    try:
        raw_json = parse_gemini_json(raw_text)
        validated = validate_and_normalize_script(raw_json, video_duration=duration_seconds)
        validated["video_metadata"] = metadata
        validated["profile"] = prof_key
        validated["persona"] = persona_key
        detected_pronoun = validated.get("speaker_pronoun") or raw_json.get("speaker_pronoun")
        validated["scenes"] = sanitize_script_pronouns(
            validated["scenes"],
            persona=persona_key,
            speaker_pronoun=detected_pronoun
        )
        # Runtime Retention QA & 1-pass targeted repair
        risks = analyze_script_retention_risk(validated["scenes"], video_duration=duration_seconds)
        validated["retention_risks"] = risks
        high_risks = [r for r in risks if r.get("risk_level") == "HIGH"]
        if high_risks:
            target_idx = high_risks[0]["scene"] - 1
            if 0 <= target_idx < len(validated["scenes"]):
                sc = validated["scenes"][target_idx]
                target_d = sc.get("target_duration") or sc.get("duration_seconds") or 3.0
                repaired = rewrite_scene_for_duration(
                    scene=sc,
                    target_duration=target_d,
                    actual_duration=target_d + 1.2,
                    api_key=api_key,
                    profile=prof_key
                )
                validated["scenes"][target_idx] = repaired
                validated = validate_and_normalize_script(validated, video_duration=duration_seconds)
                validated["retention_risks"] = analyze_script_retention_risk(validated["scenes"], video_duration=duration_seconds)
        return validated
    except Exception as e:
        logger.error(f"Lỗi validate kịch bản từ video: {e}, text: {raw_text}")
        raise RuntimeError(f"Không thể phân tích dữ liệu kịch bản từ video: {e}")


def rewrite_scene_for_duration(
    scene: Dict[str, Any],
    target_duration: float,
    actual_duration: float,
    api_key: Optional[str] = None,
    previous_scene: Optional[Dict[str, Any]] = None,
    next_scene: Optional[Dict[str, Any]] = None,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """
    AI viết lại DUY NHẤT một phân cảnh bị lệch thời lượng giọng đọc (Closed-Loop TTS Rewrite):
    - Đón nhận ngữ cảnh cảnh trước (previous_scene) và cảnh sau (next_scene) để giữ trọn mạch truyện.
    - Nếu actual_duration > target_duration (+0.3s): Cắt bớt từ thừa, rút ngắn câu, giữ trọn ý chính.
    - Nếu actual_duration < target_duration (-0.3s): Bổ sung thêm từ đệm tự nhiên, chi tiết đời thường để vừa vặn ngân sách.
    """
    delta = round(actual_duration - target_duration, 2)
    target_words = int(round(target_duration * 2.4))
    curr_text = str(scene.get("speaker_text", "")).strip()

    if abs(delta) <= 0.3:
        return dict(scene)

    context_lines = []
    if previous_scene and previous_scene.get("speaker_text"):
        context_lines.append(f"Cảnh trước (#{previous_scene.get('index')}): \"{previous_scene.get('speaker_text')}\"")
    context_lines.append(f"Cảnh hiện tại (#{scene.get('index', 1)}): \"{curr_text}\"")
    if next_scene and next_scene.get("speaker_text"):
        context_lines.append(f"Cảnh sau (#{next_scene.get('index')}): \"{next_scene.get('speaker_text')}\"")

    context_str = "\n".join(context_lines)

    if delta > 0:
        instruction = (
            f"Lời thoại hiện tại ({len(curr_text.split())} từ) khi đọc bằng TTS bị DÀI hơn {delta}s "
            f"(đọc mất {actual_duration:.1f}s trong khi chỉ có {target_duration:.1f}s). "
            f"Hãy rút ngắn lại còn đúng khoảng {target_words} từ, bỏ từ rườm rà, nhịp câu gãy gọn "
            f"nhưng vẫn tự nhiên, ăn khớp với mạch truyện của các cảnh xung quanh và giữ đúng thông điệp."
        )
    else:
        instruction = (
            f"Lời thoại hiện tại ({len(curr_text.split())} từ) khi đọc bằng TTS bị NGẮN hơn {abs(delta)}s "
            f"(đọc chỉ mất {actual_duration:.1f}s trong khi có ngân sách {target_duration:.1f}s). "
            f"Hãy thêm một vài từ đệm cảm thán hoặc một chi tiết đời thường để lời thoại đạt khoảng {target_words} từ, "
            f"nói chậm rãi, tự nhiên hơn và giữ mạch kể liền mạch với các cảnh xung quanh."
        )

    prompt = f"""Ngữ cảnh các phân cảnh:
{context_str}

Yêu cầu cho cảnh #{scene.get('index', 1)}:
{instruction}

{HUMAN_VOICE_GUIDELINES}

BẮT BUỘC chỉ trả về DUY NHẤT một JSON:
{{
  "speaker_text": "Lời thoại mới đã tinh chỉnh...",
  "text_overlay": "{scene.get('text_overlay', '')}"
}}"""

    system_instruction = (
        "Bạn là chuyên gia biên tập kịch bản video ngắn, chuyên căn chỉnh độ dài lời thoại "
        "cho vừa khít nhịp thở và thời lượng phát thanh, bảo đảm tính liên kết ngữ cảnh chặt chẽ."
    )
    try:
        raw = call_gemini(
            prompt,
            api_key=api_key,
            system_instruction=system_instruction,
            response_schema=SCENE_REWRITE_SCHEMA
        )
        if raw:
            parsed = parse_gemini_json(raw)
            new_text = str(parsed.get("speaker_text") or "").strip()
            if new_text:
                new_scene = dict(scene)
                new_scene["speaker_text"] = new_text
                new_scene["word_count"] = len(new_text.split())
                new_scene["text_overlay"] = parsed.get("text_overlay") or scene.get("text_overlay", "")
                new_scene["estimated_duration"] = round(len(new_text.split()) / 2.4, 1)
                return new_scene
    except Exception as e:
        logger.warning(f"Không thể rewrite phân cảnh #{scene.get('index')}: {e}")

    return dict(scene)


def analyze_script_retention_risk(scenes: List[Dict[str, Any]], video_duration: float) -> List[Dict[str, Any]]:
    """Phân tích các rủi ro giữ chân người xem (Retention Risk Analysis) cho từng phân cảnh."""
    risks = []
    for sc in scenes:
        idx = sc.get("index", 1)
        dur = sc.get("duration_seconds") or (float(sc.get("end_seconds", 0)) - float(sc.get("start_seconds", 0)))
        words = sc.get("word_count") or len(str(sc.get("speaker_text", "")).split())
        sec = str(sc.get("section", "")).lower()
        is_anti_cp = sc.get("is_anti_copyright", False) or (float(sc.get("target_duration") or 0) >= 4.5)

        if sec == "hook":
            if is_anti_cp:
                if dur > 10.5:
                    risks.append({
                        "scene": idx,
                        "risk_level": "HIGH",
                        "issue": "Hook quá dài (>10.5s)",
                        "recommendation": "Rút ngắn câu mở đầu dưới 10 giây để giữ nhịp độ hấp dẫn."
                    })
                elif dur > 4.0 and words / max(0.5, dur) < 1.6:
                    risks.append({
                        "scene": idx,
                        "risk_level": "MEDIUM",
                        "issue": "Hook né bản quyền nói quá chậm (<1.6 từ/giây)",
                        "recommendation": "Cần thêm từ đệm hoặc tăng nhịp để phủ kín 5-10s đầu, tránh để lộ âm thanh gốc."
                    })
            else:
                if dur > 3.5:
                    risks.append({
                        "scene": idx,
                        "risk_level": "HIGH",
                        "issue": "Hook quá dài (>3.5s)",
                        "recommendation": "Rút ngắn câu mở đầu dưới 3 giây để tránh người xem lướt qua."
                    })
        elif dur > 12.0 and words < 15:
            risks.append({
                "scene": idx,
                "risk_level": "MEDIUM",
                "issue": "Mật độ thông tin quá loãng",
                "recommendation": "Cảnh dài hơn 12s nhưng quá ít từ, dễ gây nhàm chán nếu hình ảnh không đổi góc liên tục."
            })
        elif words / max(0.5, dur) > 3.2:
            risks.append({
                "scene": idx,
                "risk_level": "MEDIUM",
                "issue": "Nói quá nhanh (>3.2 từ/giây)",
                "recommendation": "Giảm bớt từ để giọng đọc TTS không bị dồn dập, khó nghe."
            })

    return risks

