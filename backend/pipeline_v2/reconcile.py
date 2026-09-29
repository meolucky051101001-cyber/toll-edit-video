"""Reconciliation service for detecting and restoring missing dialogue/subtitles from ASR gaps."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("pipeline_v2.reconcile")


@dataclass
class ReconciledSpan:
    span_id: str
    first_time: float
    last_time: float
    start: float
    end: float
    text: str
    bbox: List[float]
    classification: str  # 'spoken_dialogue', 'unvoiced_scene_text', 'scene_packaging_or_logo', 'uncertain'
    vocal_rms_db: float
    confidence: float
    sample_count: int
    in_subtitle_band: bool
    translated_vietnamese: str = ""
    evidence_frame_time: float = 0.0


def _char_overlap(s1: str, s2: str) -> float:
    c1, c2 = set(s1.strip()), set(s2.strip())
    if not c1 or not c2:
        return 0.0
    return len(c1 & c2) / max(len(c1), len(c2))


def cluster_gap_detections(
    gap_detections: Sequence[Mapping[str, Any]],
    max_gap_seconds: float = 2.5,
    min_char_overlap: float = 0.6,
) -> List[Dict[str, Any]]:
    """Cluster raw 1fps/periodic OCR gap samples into cohesive text spans."""
    # Filter valid detections with at least 2 non-whitespace characters
    valid_dets = [
        d for d in gap_detections
        if len(str(d.get("text", "")).strip()) >= 2
    ]
    if not valid_dets:
        return []

    # Sort chronologically
    sorted_dets = sorted(valid_dets, key=lambda d: float(d.get("time", 0.0)))

    clusters: List[Dict[str, Any]] = []
    for d in sorted_dets:
        t = max(0.0, float(d.get("time", 0.0)))
        text = str(d.get("text", "")).strip()
        bbox = list(d.get("bbox", [0.0, 0.85, 1.0, 0.95]))
        prob = float(d.get("prob", 1.0) or 1.0)
        in_band = bool(d.get("in_subtitle_band", False))

        matched = False
        for c in reversed(clusters):
            time_delta = t - c["last_time"]
            if time_delta > max_gap_seconds:
                break
            same_text = (text == c["text"])
            overlap = _char_overlap(text, c["text"])
            same_zone = (c["in_subtitle_band"] == in_band)
            if same_text or (same_zone and overlap >= min_char_overlap):
                c["last_time"] = t
                c["samples"].append(d)
                if len(text) > len(c["text"]):
                    c["text"] = text
                # Union bounding box
                c["bbox"] = [
                    min(c["bbox"][0], bbox[0]),
                    min(c["bbox"][1], bbox[1]),
                    max(c["bbox"][2], bbox[2]),
                    max(c["bbox"][3], bbox[3]),
                ]
                c["in_subtitle_band"] = c["in_subtitle_band"] or in_band
                matched = True
                break

        if not matched:
            clusters.append({
                "text": text,
                "first_time": t,
                "last_time": t,
                "bbox": bbox,
                "in_subtitle_band": in_band,
                "samples": [d],
            })

    return clusters


def measure_vocal_rms(
    audio_path: Optional[Path],
    start_sec: float,
    end_sec: float,
) -> Tuple[float, str]:
    """Measure RMS in dB of the vocal stem (or mixed audio) for a specific time range.
    Returns (rms_db, status) where status is 'ok', 'silent', 'error', 'no_file'.
    """
    if not audio_path or not Path(audio_path).is_file():
        return -100.0, "no_file"

    try:
        import soundfile as sf
        with sf.SoundFile(str(audio_path)) as f:
            sr = f.samplerate
            total_frames = len(f)
            start_frame = max(0, int(start_sec * sr))
            end_frame = min(total_frames, int(end_sec * sr))
            if end_frame <= start_frame:
                return -100.0, "silent"
            f.seek(start_frame)
            data = f.read(end_frame - start_frame)
            if len(data) == 0:
                return -100.0, "silent"
            rms = float(np.sqrt(np.mean(data**2)))
            rms_db = float(20 * np.log10(rms)) if rms > 1e-9 else -100.0
            return rms_db, "ok"
    except Exception as exc:
        logger.warning(f"Could not measure vocal RMS from {audio_path}: {exc}")
        return -100.0, "unknown"


PACKAGING_KEYWORDS = (
    "净含量", "配料", "配料表", "保质期", "生产日期", "产地", "执行标准",
    "条形码", "营养成分", "规格", "重量", "容量", "品牌", "旗舰店",
    "正品", "专营店", "官方", "全国统一", "零售价", "贮存条件",
    "委托商", "制造商", "许可证", "防伪", "扫码", "批号", "适用",
    "成分", "毫升", "克", "公斤", "出品方", "监制", "招商",
    "薄荷", "莲蓉", "原料", "净重", "生产商", "地址", "电话", "保质",
    "选择主体", "移除背景", "立即生成", "背景", "主体", "抠图", "消除",
    "画质", "修复", "图层", "蒙版", "参数", "滤镜", "特效", "调色", "剪辑",
    "写真", "预设", "色调", "曝光", "对比度", "高光", "阴影", "饱和度", "清晰度", "色温"
)



def classify_text_span(
    text: str,
    in_subtitle_band: bool,
    vocal_rms_db: float,
    bbox: Sequence[float],
    audio_status: str = "ok",
    frame_persistence: float = 0.0,
) -> str:
    """Classify a text span into 4 strict categories:
    1. 'spoken_dialogue': Dialogue in subtitle band with human speech present
    2. 'unvoiced_scene_text': Subtitle-like narrative text but audio is silent
    3. 'packaging_or_watermark': Watermark, packaging label, logo, or non-subtitle text
    4. 'uncertain': Unclear text requiring manual review
    """
    cleaned = text.strip()
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', cleaned))

    # Social tag, watermark, handles
    if cleaned.startswith(("@", "*", "©", "®")) or "鹿茸" in cleaned or "抖音" in cleaned:
        return "packaging_or_watermark"

    # Non-Chinese packaging/stats (e.g. "UCN", "5,00", pure numbers/symbols)
    if not has_cjk or re.match(r'^(?:[0-9\.,\s\+%\-\/:]+|UCN|HD|kg|g|ml|fps)$', cleaned, re.IGNORECASE):
        return "packaging_or_watermark"

    # Explicit packaging keywords even if located in bottom subtitle band
    # Narrative time cards (e.g. "两个小时前", "三年后", "清晨", "半小时前，挖坑抓鱼")
    is_narrative = bool(re.search(r'(?:小时前|分钟前|天前|年后|个月前|清晨|深夜|傍晚|剧终|全剧终)', cleaned))
    if is_narrative:
        return "unvoiced_scene_text"

    # Explicit packaging keywords even if located in bottom subtitle band
    if any(kw in cleaned for kw in PACKAGING_KEYWORDS):
        return "packaging_or_watermark"

    # Static persistence (> 8s continuous presence) is characteristic of watermark or packaging
    if frame_persistence >= 8.0:
        return "packaging_or_watermark"

    # Reject OCR artifacts with fewer than 2 CJK characters (e.g. '国5n')
    cjk_chars = re.findall(r'[\u4e00-\u9fff]', cleaned)
    if len(cjk_chars) < 2:
        return "packaging_or_watermark"

    if len(bbox) >= 4:
        raw_w = 1080.0 if (bbox[2] > 1.0 or bbox[3] > 1.0) else 1.0
        norm_x1 = bbox[0] / raw_w
        norm_x2 = bbox[2] / raw_w
        norm_y1 = bbox[1] / (1920.0 if (bbox[3] > 1.0) else 1.0)
        cx = (norm_x1 + norm_x2) / 2.0
        box_w = abs(norm_x2 - norm_x1)
    elif len(bbox) >= 1:
        cx = bbox[0] if bbox[0] <= 1.0 else bbox[0] / 1080.0
        norm_y1 = 0.85
        box_w = 0.5
    else:
        cx = 0.5
        norm_y1 = 0.85
        box_w = 0.5

    is_centered = abs(cx - 0.5) <= 0.20

    # Reject tiny packaging labels / stickers (consistent with QC detection)
    if not is_narrative:
        if box_w < 0.06 or (len(cleaned) <= 3 and box_w < 0.10):
            return "packaging_or_watermark"

    # Lower area of screen (bottom 35%: y >= 0.65): where subtitles appear
    is_lower_screen = (in_subtitle_band or (len(bbox) >= 2 and norm_y1 >= 0.65))

    if is_lower_screen and is_centered:
        # If audio reading had an error or unknown status, fail-closed: do not assume silent!
        if audio_status in ("unknown", "error"):
            return "uncertain"

        # If human speech is audible in vocal stem (RMS >= -44 dB) or substantial dialogue
        if vocal_rms_db >= -44.0 or (vocal_rms_db >= -48.0 and len(cleaned) >= 3):
            return "spoken_dialogue"
        # If audio is completely silent and status is verified ok, it's on-screen narrative
        return "unvoiced_scene_text"

    # Off-center text or text in upper/mid screen (bbox[1] < 0.65):
    # Absolute rule: NEVER classify as spoken_dialogue!
    if is_narrative:
        return "unvoiced_scene_text"

    return "packaging_or_watermark"


def reconcile_asr_gaps(
    gap_detections: Sequence[Mapping[str, Any]],
    existing_segments: Sequence[Any],
    vocal_audio_path: Optional[Path],
    video_duration: float = 0.0,
) -> Tuple[List[ReconciledSpan], List[Dict[str, Any]]]:
    """Reconcile gap detections against existing ASR segments, produce validated spans."""
    clusters = cluster_gap_detections(gap_detections)
    reconciled_spans: List[ReconciledSpan] = []

    # Map existing speech intervals and text content
    speech_intervals = []
    speech_records = []
    for s in existing_segments:
        s_val = getattr(s, "start", 0.0) if hasattr(s, "start") else s.get("start", 0.0)
        e_val = getattr(s, "end", s_val) if hasattr(s, "end") else s.get("end", s_val)
        st = float(s_val.total_seconds()) if hasattr(s_val, "total_seconds") else float(s_val or 0.0)
        et = float(e_val.total_seconds()) if hasattr(e_val, "total_seconds") else float(e_val or st)
        text_val = (
            getattr(s, "orig_content", None)
            or getattr(s, "content", None)
            or getattr(s, "text", "")
            or (s.get("orig_content") if isinstance(s, dict) else "")
            or (s.get("content") if isinstance(s, dict) else "")
            or (s.get("text") if isinstance(s, dict) else "")
            or ""
        )
        speech_intervals.append((st, et))
        speech_records.append((st, et, str(text_val).strip()))

    for idx, c in enumerate(clusters, 1):
        cleaned_text = str(c["text"]).strip()
        is_narrative = bool(re.search(r'(?:小时前|分钟前|天前|年后|个月前|清晨|深夜|傍晚|剧终|全剧终)', cleaned_text))
        conf = float(np.mean([float(s.get("prob", 1.0) or 1.0) for s in c["samples"]]))

        # Filter out low-confidence single-frame noise
        if len(c["samples"]) == 1 and conf < 0.75 and not is_narrative:
            continue

        # Check if text is redundant / duplicate of an adjacent ASR segment (e.g. within 2.5s)
        is_asr_duplicate = False
        for s_st, s_et, s_text in speech_records:
            if not s_text:
                continue
            if abs(c["first_time"] - s_et) <= 2.5 or abs(c["last_time"] - s_st) <= 2.5 or (s_st <= c["last_time"] and c["first_time"] <= s_et):
                overlap = _char_overlap(cleaned_text, s_text)
                if overlap >= 0.40 or (len(cleaned_text) >= 2 and cleaned_text in s_text) or (len(s_text) >= 2 and s_text in cleaned_text):
                    is_asr_duplicate = True
                    break
        if is_asr_duplicate:
            logger.info("Skipping redundant OCR gap text '%s' - matches adjacent ASR segment '%s'", cleaned_text, s_text)
            continue

        span_sample_times = sorted([float(s.get("time", 0.0)) for s in c["samples"]])
        sample_stride = np.median(np.diff(span_sample_times)) if len(span_sample_times) > 1 else 0.5
        onset_pad = min(0.35, max(0.18, float(sample_stride) * 0.8))
        raw_start = max(0.0, c["first_time"] - onset_pad)
        end_pad = min(0.40, max(0.18, float(sample_stride) * 0.8))
        raw_end = c["last_time"] + end_pad
        if video_duration > 0:
            raw_end = min(video_duration, raw_end)

        # Bound start and end so they don't severely overlap adjacent speech segments
        start_sec = raw_start
        end_sec = raw_end

        # Check adjacent intervals
        for s_st, s_et in speech_intervals:
            # If span starts inside an existing segment, bump start
            if s_st - 0.05 <= start_sec < s_et:
                start_sec = max(start_sec, s_et + 0.02)
            # If span ends inside an existing segment, clamp end
            if s_st < end_sec <= s_et + 0.05:
                end_sec = min(end_sec, s_st - 0.02)

        # If after clamping, no space remains (<= 0.15s), do NOT fabricate a fake micro-segment!
        if end_sec <= start_sec + 0.15:
            continue

        # Expand into available preceding free space if spoken dialogue needs voicing room
        if end_sec - start_sec < 0.8:
            prev_speech_end = max([s_et for s_st, s_et in speech_intervals if s_et <= start_sec], default=0.0)
            if start_sec - prev_speech_end > 0.05:
                needed = 0.8 - (end_sec - start_sec)
                expand = min(needed, start_sec - (prev_speech_end + 0.02))
                if expand > 0:
                    start_sec = round(start_sec - expand, 3)

        v_db, audio_status = measure_vocal_rms(vocal_audio_path, start_sec, end_sec) if vocal_audio_path else (-100.0, "no_file")
        in_band = bool(c["in_subtitle_band"] or (len(c["bbox"]) >= 2 and c["bbox"][1] >= 0.70))
        persistence = float(c["last_time"] - c["first_time"])
        classification = classify_text_span(
            c["text"],
            in_band,
            v_db,
            c["bbox"],
            audio_status=audio_status,
            frame_persistence=persistence,
        )

        # Spoken dialogue requires at least 0.9s for TTS voicing.
        # If available slot is < 0.9s, downgrade to unvoiced_scene_text so it displays subtitle without voice overflow.
        if classification == "spoken_dialogue" and (end_sec - start_sec) < 0.9:
            classification = "unvoiced_scene_text"

        evidence_time = round((c["first_time"] + c["last_time"]) / 2.0, 2)

        span = ReconciledSpan(
            span_id=f"gap_span_{idx:04d}",
            first_time=round(c["first_time"], 3),
            last_time=round(c["last_time"], 3),
            start=round(start_sec, 3),
            end=round(end_sec, 3),
            text=c["text"],
            bbox=c["bbox"],
            classification=classification,
            vocal_rms_db=round(v_db, 2),
            confidence=round(conf, 3),
            sample_count=len(c["samples"]),
            in_subtitle_band=in_band,
            evidence_frame_time=evidence_time,
        )
        reconciled_spans.append(span)

    # Prevent overlapping and eliminate coverage gaps between adjacent dialogue/subtitle spans
    sub_spans = [s for s in reconciled_spans if s.classification in ("spoken_dialogue", "unvoiced_scene_text")]
    for i in range(len(sub_spans) - 1):
        curr_span = sub_spans[i]
        next_span = sub_spans[i + 1]
        gap_between_samples = next_span.first_time - curr_span.last_time
        curr_cy = (curr_span.bbox[1] + curr_span.bbox[3]) / 2.0
        next_cy = (next_span.bbox[1] + next_span.bbox[3]) / 2.0
        same_y = abs(curr_cy - next_cy) <= 0.04

        if same_y:
            # Same vertical position: continuous subtitle transition
            if 0 <= gap_between_samples <= 0.8:
                if curr_span.end < next_span.start:
                    curr_span.end = next_span.start
                elif curr_span.end > next_span.start:
                    mid = round((curr_span.end + next_span.start) / 2.0, 3)
                    curr_span.end = max(curr_span.start + 0.2, mid)
                    next_span.start = min(next_span.end - 0.2, mid)
        else:
            # Different vertical position (e.g. side-note vs bottom subtitle):
            # Never delay next_span.start; if curr_span overlaps into next_span, clamp curr_span.end
            if curr_span.end > next_span.start:
                curr_span.end = max(curr_span.start + 0.2, next_span.start)

    return reconciled_spans, [s.__dict__ for s in reconciled_spans]


from .domain import (
    DubUtterance,
    JobTranslationOverrideStore,
    SourceTextTrack,
    VietnameseSubtitle,
)

GENERIC_INTERJECTIONS: Mapping[str, str] = {
    "哈哈": "Ha ha",
    "哈哈哈": "Ha ha ha",
    "嘿嘿": "Hê hê",
    "嘿嘿嘿": "Hê hê hê",
    "呼呼": "Phù phù",
    "哎哎": "Ê ê",
    "哎呀": "Ái chà",
    "啊": "A",
    "嗯嗯": "Ưm ưm",
}


def _norm_cjk(s: str) -> str:
    """Normalize string for dictionary lookup by stripping punctuation and whitespaces."""
    return re.sub(r'[\s，。！？!?.,、\-_:;]+', '', s.strip())


_NORM_GENERIC_INTERJECTIONS = {
    _norm_cjk(k): v for k, v in GENERIC_INTERJECTIONS.items()
}


async def translate_spans(
    spans: Sequence[ReconciledSpan],
    target_lang: str = "vi",
    api_key: str = "",
    glossary: Optional[Mapping[str, str]] = None,
    entity_map: Optional[Mapping[str, str]] = None,
    translation_overrides: Optional[Mapping[str, str]] = None,
    video_hash: str = "",
    override_store: Optional[JobTranslationOverrideStore] = None,
) -> None:
    """Translate dialogue and scene text spans into Vietnamese without video-specific hardcoded text."""
    import asyncio
    to_translate = [s for s in spans if s.classification in ("spoken_dialogue", "unvoiced_scene_text")]
    if not to_translate:
        return

    # Load default job overrides store if not explicitly passed
    if override_store is None:
        default_override_file = Path(__file__).resolve().parent.parent / "data" / "job_overrides.json"
        if default_override_file.is_file():
            override_store = JobTranslationOverrideStore.from_file(default_override_file)

    unresolved_indices = []
    norm_overrides = {_norm_cjk(k): v for k, v in (translation_overrides or {}).items()}
    norm_glossary = {_norm_cjk(k): v for k, v in (glossary or {}).items()}

    for idx, s in enumerate(to_translate):
        clean = s.text.strip()
        norm = _norm_cjk(clean)

        # 1. Direct memory overrides
        if translation_overrides and clean in translation_overrides:
            s.translated_vietnamese = translation_overrides[clean]
        elif norm in norm_overrides:
            s.translated_vietnamese = norm_overrides[norm]
        # 2. Per-job verified override store strictly scoped by video_hash
        elif override_store and video_hash:
            ov = override_store.get_override(video_hash, source_text=clean, track_id=s.span_id)
            if ov:
                s.translated_vietnamese = ov.override_translation
                if ov.override_classification:
                    s.classification = ov.override_classification
            elif glossary and clean in glossary:
                s.translated_vietnamese = glossary[clean]
            elif norm in norm_glossary:
                s.translated_vietnamese = norm_glossary[norm]
            elif norm in _NORM_GENERIC_INTERJECTIONS:
                s.translated_vietnamese = _NORM_GENERIC_INTERJECTIONS[norm]
            else:
                unresolved_indices.append(idx)
        # 3. Glossary & generic interjections
        elif glossary and clean in glossary:
            s.translated_vietnamese = glossary[clean]
        elif norm in norm_glossary:
            s.translated_vietnamese = norm_glossary[norm]
        elif norm in _NORM_GENERIC_INTERJECTIONS:
            s.translated_vietnamese = _NORM_GENERIC_INTERJECTIONS[norm]
        else:
            unresolved_indices.append(idx)

    if not unresolved_indices:
        return

    texts = [to_translate[i].text for i in unresolved_indices]
    translated = None

    if api_key:
        try:
            from ai.translation import translate_with_gemini
            translated = await asyncio.to_thread(
                translate_with_gemini,
                texts,
                target_lang=target_lang,
                api_key=api_key,
                glossary=glossary,
                entity_map=entity_map,
            )
        except Exception as exc:
            logger.warning(f"Gemini translation for gap spans failed: {exc}, falling back...")

    if not translated or len(translated) != len(texts):
        try:
            from ai.translation import _translate_with_resilient_fallback
            translated = []
            for t in texts:
                tr = await asyncio.to_thread(_translate_with_resilient_fallback, t, target_lang)
                translated.append(tr or t)
        except Exception as exc:
            logger.warning(f"Fallback translation for gap spans failed: {exc}")
            translated = texts

    for idx, trans in zip(unresolved_indices, translated):
        to_translate[idx].translated_vietnamese = trans.strip() if trans else to_translate[idx].text


def build_domain_tracks(
    spans: Sequence[ReconciledSpan],
    existing_segments: Sequence[Any],
) -> Tuple[List[SourceTextTrack], List[VietnameseSubtitle], List[DubUtterance]]:
    """Build standardized SourceTextTrack, VietnameseSubtitle, and DubUtterance instances."""
    source_tracks: List[SourceTextTrack] = []
    subtitles: List[VietnameseSubtitle] = []
    dub_utterances: List[DubUtterance] = []

    # 1. Existing speech segments from ASR/Translation
    for seg in existing_segments:
        idx = getattr(seg, "index", 0)
        s_val = getattr(seg, "start", 0.0)
        e_val = getattr(seg, "end", s_val)
        st = float(s_val.total_seconds()) if hasattr(s_val, "total_seconds") else float(s_val or 0.0)
        et = float(e_val.total_seconds()) if hasattr(e_val, "total_seconds") else float(e_val or st)
        orig_text = str(getattr(seg, "orig_content", "") or getattr(seg, "content", "") or "")
        trans_text = str(getattr(seg, "content", "") or "")
        gender = str(getattr(seg, "gender", "female") or "female")
        speaker_id = str(getattr(seg, "speaker_id", f"spk_{idx}") or f"spk_{idx}")
        best_block = getattr(seg, "best_block", None)

        bbox_tuple = None
        if best_block:
            bbox_tuple = (
                float(getattr(best_block, "x_pct", 0.0)),
                float(getattr(best_block, "y_pct", 0.0)),
                float(getattr(best_block, "max_x_pct", 1.0)),
                float(getattr(best_block, "max_y_pct", 1.0)),
            )

        trk_id = f"trk_asr_{idx:04d}"
        source_tracks.append(
            SourceTextTrack(
                track_id=trk_id,
                raw_text=orig_text,
                start_time=st,
                end_time=et,
                best_bbox=bbox_tuple,
                confidence=1.0,
                classification="spoken_dialogue",
                origin="asr",
                source_segment_id=idx,
            )
        )

        sub_id = f"sub_{idx:04d}"
        subtitles.append(
            VietnameseSubtitle(
                sub_id=sub_id,
                source_track_id=trk_id,
                translated_text=trans_text,
                display_text=trans_text,
                start_time=st,
                end_time=et,
                is_unvoiced=False,
            )
        )

        dub_utterances.append(
            DubUtterance(
                utterance_id=f"utt_{idx:04d}",
                source_track_id=trk_id,
                speaker_id=speaker_id,
                gender=gender,
                start_time=st,
                end_time=et,
                artifact_key=f"rvc/{idx}.wav",
                is_silent_placeholder=False,
            )
        )

    # 2. Reconciled spans from gap OCR
    for s in spans:
        trk_id = f"trk_{s.span_id}"
        bbox_tuple = tuple(s.bbox) if len(s.bbox) == 4 else (0.0, 0.7, 1.0, 0.95)
        source_tracks.append(
            SourceTextTrack(
                track_id=trk_id,
                raw_text=s.text,
                start_time=s.start,
                end_time=s.end,
                best_bbox=bbox_tuple,
                confidence=s.confidence,
                classification=s.classification,
                origin="reconciled",
                needs_review=(s.classification == "uncertain" or s.confidence < 0.65),
                review_reason="Audio status unknown or OCR confidence borderline - marked for review" if (s.classification == "uncertain" or s.confidence < 0.65) else None,
            )
        )

        if s.classification in ("spoken_dialogue", "unvoiced_scene_text") and s.translated_vietnamese:
            is_unvoiced = (s.classification == "unvoiced_scene_text")
            sub_id = f"sub_{s.span_id}"
            subtitles.append(
                VietnameseSubtitle(
                    sub_id=sub_id,
                    source_track_id=trk_id,
                    translated_text=s.translated_vietnamese,
                    display_text=s.translated_vietnamese,
                    start_time=s.start,
                    end_time=s.end,
                    is_unvoiced=is_unvoiced,
                )
            )

            dub_utterances.append(
                DubUtterance(
                    utterance_id=f"utt_{s.span_id}",
                    source_track_id=trk_id,
                    speaker_id="reconciled",
                    gender="female",
                    start_time=s.start,
                    end_time=s.end,
                    is_silent_placeholder=is_unvoiced,
                )
            )

    return source_tracks, subtitles, dub_utterances


def build_segments_from_spans(spans: Sequence[ReconciledSpan]) -> List[Any]:
    """Create RuntimeSegment objects from translated dialogue and unvoiced scene spans."""
    from datetime import timedelta
    from .segments import GeometryBlock, RuntimeSegment

    new_segments = []
    for s in spans:
        if s.classification not in ("spoken_dialogue", "unvoiced_scene_text"):
            continue
        if not s.translated_vietnamese:
            continue

        block = GeometryBlock(
            text=s.text,
            start=s.start,
            end=s.end,
            x_pct=s.bbox[0],
            max_x_pct=s.bbox[2],
            y_pct=s.bbox[1],
            max_y_pct=s.bbox[3],
            prob=s.confidence,
            is_subtitle=True,
            is_packaging=False,
            is_static=False,
            in_subtitle_band=s.in_subtitle_band,
            type="subtitle",
        )

        seg = RuntimeSegment(
            index=0,
            start=timedelta(seconds=s.start),
            end=timedelta(seconds=s.end),
            content=s.translated_vietnamese,
            orig_content=s.text,
            source_segment_id=0,
            y_pct=s.bbox[1],
            max_y_pct=s.bbox[3],
            best_block=block,
            tracking_blocks=[block],
            gender="female",
            speaker_id="reconciled",
            is_subtitle=True,
            is_packaging=False,
            is_static=False,
            in_subtitle_band=s.in_subtitle_band,
        )
        if s.classification == "unvoiced_scene_text" or (s.end - s.start) < 0.75:
            setattr(seg, "is_unvoiced_scene", True)
            setattr(seg, "is_silent_fallback", False)
        setattr(seg, "is_gap_reconciled", True)
        new_segments.append(seg)

    return new_segments

