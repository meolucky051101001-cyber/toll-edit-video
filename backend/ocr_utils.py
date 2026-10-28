import cv2
import logging
import os
import tempfile
import hashlib
import copy
import math
from datetime import timedelta
from collections import OrderedDict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import srt
try:
    from .v1_ocr_proxy import ocr_proxy
except ImportError:
    from v1_ocr_proxy import ocr_proxy
try:
    from .v1_stage_metrics import stage
except ImportError:
    from v1_stage_metrics import stage

try:
    from .ai.v1_model_policy import current_v1_model_policy
    from .ai.v1_model_runtime import V1ModelRuntimeError, run_v1_ocr, runtime_module_available
except ImportError:
    from ai.v1_model_policy import current_v1_model_policy
    from ai.v1_model_runtime import V1ModelRuntimeError, run_v1_ocr, runtime_module_available

try:
    from .ocr_subtitle_locator import select_chinese_subtitle_band
except ImportError:
    from ocr_subtitle_locator import select_chinese_subtitle_band

try:
    from .v1_ocr_geometry import (value, sample_times, rescue_times, has_caption_evidence,
                                  geometry_summary, validate_mask_geometry, normalized_rows)
except ImportError:
    from v1_ocr_geometry import (value, sample_times, rescue_times, has_caption_evidence,
                                geometry_summary, validate_mask_geometry, normalized_rows)


logger = logging.getLogger(__name__)
reader = None
_paddle_failed = False
_frame_cache = OrderedDict()

def get_ocr_reader():
    global reader
    if reader is None:
        logger.info("Initializing EasyOCR reader (CPU mode for safe stability)...")
        # Dùng CPU cho EasyOCR để tránh lỗi xung đột cudnnGetLibConfig Error code 127
        import easyocr

        reader = easyocr.Reader(['ch_sim'], gpu=False)
    return reader

def _close_ocr_session():
    import sys
    called = set()
    for mod_name in ("backend.ai.v1_model_runtime", "ai.v1_model_runtime"):
        mod = sys.modules.get(mod_name)
        fn = getattr(mod, "close_session", None) if mod is not None else None
        if callable(fn) and id(fn) not in called:
            called.add(id(fn))
            fn()
    if not called:
        try:
            from .ai.v1_model_runtime import close_session
        except ImportError:
            from ai.v1_model_runtime import close_session
        close_session()


def release_ocr_reader(force_close_session: bool = False):
    global reader, _paddle_failed
    was_failed = _paddle_failed
    _paddle_failed = False
    _frame_cache.clear()
    keep_warm = os.getenv("V1_OCR_KEEP_WARM", "1").strip().lower() not in ("0", "false", "no")
    if force_close_session or was_failed or not keep_warm:
        _close_ocr_session()
    if reader is not None:
        logger.info("🧹 Đang giải phóng bộ nhớ RAM/VRAM của EasyOCR...")
        del reader
        reader = None
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _has_chinese_overlap(candidate_text: str, segment_text: str) -> bool:
    """Check whether OCR and ASR share meaningful Chinese characters."""
    candidate = "".join(char for char in str(candidate_text or "") if "\u3400" <= char <= "\u9fff")
    segment = "".join(char for char in str(segment_text or "") if "\u3400" <= char <= "\u9fff")
    if not candidate or not segment:
        return False
    shared = set(candidate) & set(segment)
    if len(shared) >= 2 or (len(candidate) >= 2 and len(shared) / len(candidate) >= 0.3):
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, candidate, segment).ratio() >= 0.3


def _readtext_batch(frames):
    # Reuse only pixel-identical frames, never merely similar subtitle regions:
    # no risk of missing a changed Chinese glyph or motion.
    if not all(hasattr(frame, "tobytes") for frame in frames):
        return _recognize_batch(frames)
    keys = [hashlib.sha256(str(frame.shape).encode() + frame.tobytes()).digest()
            for frame in frames]
    pending = {}
    for key, frame in zip(keys, frames):
        if key not in _frame_cache:
            pending.setdefault(key, frame)
    if pending:
        rows = _recognize_batch(list(pending.values()))
        if len(rows) != len(pending):
            raise RuntimeError("OCR returned incomplete unique frames")
        for key, row in zip(pending, rows):
            _frame_cache[key] = copy.deepcopy(row)
    result = [copy.deepcopy(_frame_cache[key]) for key in keys]
    while len(_frame_cache) > 64:
        _frame_cache.popitem(last=False)
    logger.info("V1 OCR frames=%d unique_work=%d reused=%d",
                len(frames), len(pending), len(frames)-len(pending))
    return result


@stage("ocr_recognize_batch")
def _recognize_batch(frames):
    """Recognize sampled frames with PP-OCRv6 Tiny, then fall back safely."""

    global _paddle_failed
    policy = current_v1_model_policy()
    ocr_backend = getattr(policy, "ocr_backend", "auto")
    if not _paddle_failed and ocr_backend in {"auto", "paddle"} and runtime_module_available(
        "paddleocr", policy
    ):
        try:
            with tempfile.TemporaryDirectory(prefix="autodub-v1-ocr-frames-") as temporary:
                paths = []
                png_params = [int(getattr(cv2, "IMWRITE_PNG_COMPRESSION", 16)), 1]
                for index, frame in enumerate(frames):
                    path = Path(temporary) / "frame_{:04d}.png".format(index)
                    if not cv2.imwrite(str(path), frame, png_params):
                        raise OSError("Could not write OCR frame {}".format(path))
                    paths.append(str(path))

                result = run_v1_ocr(
                    {
                        "images": paths,
                        "detection_model": getattr(policy, "paddle_detection_model", "PP-OCRv5_mobile_det"),
                        "recognition_model": getattr(policy, "paddle_recognition_model", "PP-OCRv5_mobile_rec"),
                        "engine": getattr(policy, "paddle_engine", "onnx"),
                    },
                    timeout_seconds=float(
                        os.getenv("V1_OCR_TIMEOUT_SECONDS", "900")
                    ),
                    policy=policy,
                )
                by_path = {
                    str(item.get("path")): item.get("rows", [])
                    for item in result.get("images", [])
                }
                if any(path not in by_path for path in paths):
                    raise RuntimeError("OCR response omitted a sampled frame")
                output = []
                for path in paths:
                    rows = []
                    for item in by_path.get(path, []):
                        bbox = item.get("bbox", [])
                        text = str(item.get("text", "") or "")
                        score = float(item.get("score", 0.0) or 0.0)
                        if len(bbox) >= 4 and text.strip():
                            rows.append((bbox, text, score))
                    output.append(rows)
                if len(output) != len(frames):
                    raise RuntimeError("PP-OCRv6 returned an incomplete frame batch")
                logger.info(
                    "PP-OCRv6 Tiny completed %d sampled frames via %s",
                    len(frames),
                    policy.paddle_engine,
                )
                return output
        except (V1ModelRuntimeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            # Close the failed model BEFORE opening the CPU fallback. Stay on
            # fallback until this video ends instead of loading both repeatedly.
            _close_ocr_session()
            _paddle_failed = True
            logger.error("PP-OCRv6 GPU failed; CPU fallback disabled: %s", exc)

    raise RuntimeError("V1 GPU OCR unavailable; CPU EasyOCR fallback disabled")
    easy_reader = get_ocr_reader()
    return [
        easy_reader.readtext(
            frame,
            detail=1,
            paragraph=False,
            mag_ratio=1.0,
            width_ths=0.7,
        )
        for frame in frames
    ]


def extract_silent_subtitles_from_gaps(gap_segments, target_lang="vi", api_key=None):
    return []

def stabilize_samples(samples):
    """Lock small OCR jitter without freezing real subtitle motion."""
    result = [dict(row) for row in samples]
    groups = []
    keys = ("x_pct", "max_x_pct", "y_pct", "max_y_pct")
    for row in result:
        group = groups[-1] if groups else []
        compatible = bool(group) and row["sample_time"] - group[-1]["sample_time"] <= .75001
        if compatible:
            for key in keys:
                tolerance = .025 if "x_pct" in key else .012
                values = [item[key] for item in group] + [row[key]]
                if max(values) - min(values) > tolerance:
                    compatible = False
                    break
        if compatible:
            group.append(row)
        else:
            groups.append([row])
    for group in groups:
        bounds = {key: (max if key.startswith("max_") else min)(
            row[key] for row in group) for key in keys}
        for row in group:
            row.update(bounds)
    return result


class OCRBlock:
    """OCR text with temporal and screen geometry used by subtitle/QC stages."""

    def __init__(
        self,
        text,
        start,
        end,
        x_pct,
        max_x_pct,
        y_pct,
        max_y_pct,
        prob=1.0,
        sample_segment_id=None,
        sample_time=0.0,
        is_subtitle=None,
        is_packaging=None,
        is_static=None,
        in_subtitle_band=None,
        type=None,
    ):
        self.text = text
        self.start = start
        self.end = end
        self.x_pct = x_pct
        self.max_x_pct = max_x_pct
        self.y_pct = y_pct
        self.max_y_pct = max_y_pct
        self.prob = prob
        self.sample_segment_id = sample_segment_id
        self.sample_time = sample_time
        self.is_subtitle = is_subtitle
        self.is_packaging = is_packaging
        self.is_static = is_static
        self.in_subtitle_band = in_subtitle_band
        self.type = type


def _ocr_field(block, name, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _visual_chinese_count(text):
    return sum(1 for char in str(text or "") if "\u3400" <= char <= "\u9fff")


def _visual_watermark_text(text):
    raw = str(text or "")
    if raw.lstrip().startswith(("@", "＠")):
        return True
    return any(token in raw for token in ("抖音", "小红书", "快手", "关注", "点赞", "收藏"))


def _visual_caption_key(text):
    return "".join(
        char.lower()
        for char in str(text or "")
        if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def _same_visual_caption(first, second, threshold=0.78):
    """Match OCR jitter and typing effects without joining different captions."""
    left = _visual_caption_key(first)
    right = _visual_caption_key(second)
    if not left or not right:
        return False
    if left == right:
        return True
    if SequenceMatcher(None, left, right, autojunk=False).ratio() >= threshold:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 2 and longer.startswith(shorter)


def _merged_span(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 0.2:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


def _visual_grid_times(segments, duration, step=0.35):
    if duration <= 0:
        return []
    intervals = []
    for seg in segments or []:
        try:
            start = max(0.0, seg.start.total_seconds())
            end = min(duration, seg.end.total_seconds())
        except Exception:
            continue
        if end > start:
            intervals.append((start, end))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    windows = []
    if not merged:
        windows = [(0.0, duration)]
    else:
        cursor = 0.0
        for start, end in merged:
            if start - cursor >= 1.8:
                windows.append((cursor, start))
            cursor = max(cursor, end)
        if duration - cursor >= 1.8:
            windows.append((cursor, duration))
        if not windows:
            windows = [(0.0, duration)]

    times = []
    for start, end in windows:
        current = max(0.0, start)
        while current < end - 0.0001:
            times.append(round(current, 3))
            current += step
    max_samples = max(100, int(os.getenv("V1_VISUAL_OCR_MAX_SAMPLES", "20000")))
    if len(times) > max_samples:
        logger.warning("Visual OCR grid capped at %d/%d samples", max_samples, len(times))
        times = times[:max_samples]
    return times


def _best_cluster_samples(cluster):
    by_time = {}
    for block in cluster:
        try:
            sample_time = round(float(_ocr_field(block, "sample_time", 0.0) or 0.0), 4)
        except (TypeError, ValueError):
            sample_time = 0.0
        previous = by_time.get(sample_time)
        if previous is None:
            by_time[sample_time] = block
            continue
        score = (
            float(_ocr_field(block, "prob", 0.0) or 0.0),
            len(str(_ocr_field(block, "text", "") or "")),
        )
        previous_score = (
            float(_ocr_field(previous, "prob", 0.0) or 0.0),
            len(str(_ocr_field(previous, "text", "") or "")),
        )
        if score > previous_score:
            by_time[sample_time] = block
    return [by_time[key] for key in sorted(by_time)]


def _tracking_blocks_for_cluster(cluster, start_t, end_t, band, visual_mode):
    # A caption may occupy two text lines in the same sampled frame, so keep
    # every distinct line per sample instead of collapsing to one block.
    by_time = {}
    for sample in cluster:
        sample_time = round(float(_ocr_field(sample, "sample_time", 0.0) or 0.0), 3)
        key = _visual_caption_key(_ocr_field(sample, "text", "")) or str(
            _ocr_field(sample, "text", "") or ""
        )
        line = by_time.setdefault(sample_time, {})
        previous = line.get(key)
        if previous is None or (
            float(_ocr_field(sample, "prob", 0.0) or 0.0),
            len(str(_ocr_field(sample, "text", "") or "")),
        ) > (
            float(_ocr_field(previous, "prob", 0.0) or 0.0),
            len(str(_ocr_field(previous, "text", "") or "")),
        ):
            line[key] = sample
    times = sorted(by_time)
    if not times:
        return []
    default_top = float(getattr(band, "top", 0.75) or 0.75)
    default_bottom = float(getattr(band, "bottom", 0.82) or 0.82)
    blocks = []
    for position, sample_time in enumerate(times):
        left = start_t if position == 0 else (times[position - 1] + sample_time) * 0.5
        right = end_t if position == len(times) - 1 else (sample_time + times[position + 1]) * 0.5
        left = max(start_t, left)
        right = min(end_t, right)
        if right <= left:
            continue
        for sample in by_time[sample_time].values():
            def number(name, default):
                raw = _ocr_field(sample, name, None)
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return default

            x_pct = number("x_pct", 0.05)
            max_x_pct = number("max_x_pct", 0.95)
            y_pct = number("y_pct", default_top)
            max_y_pct = number("max_y_pct", default_bottom)
            if not (0.0 <= x_pct < max_x_pct <= 1.0):
                x_pct, max_x_pct = 0.05, 0.95
            if not (0.0 <= y_pct < max_y_pct <= 1.0):
                y_pct, max_y_pct = default_top, default_bottom
            block = OCRBlock(
                text=str(_ocr_field(sample, "text", "") or ""),
                start=left,
                end=right,
                x_pct=x_pct,
                max_x_pct=max_x_pct,
                y_pct=y_pct,
                max_y_pct=max_y_pct,
                prob=number("prob", 0.85),
                sample_segment_id=_ocr_field(sample, "sample_segment_id"),
                sample_time=sample_time,
                is_subtitle=True,
                in_subtitle_band=True,
                is_packaging=False,
                is_static=False,
                type="subtitle",
            )
            block.geometry_verified = bool(visual_mode)
            block.visual_locked = bool(visual_mode)
            blocks.append(block)
    return blocks


def _tracking_ocr_block(source, start_t, end_t, default_block):
    """Normalize dict/OCRBlock samples into a real OCRBlock cover interval."""
    def number(name, default):
        try:
            return float(_ocr_field(source, name, default))
        except (TypeError, ValueError):
            return float(default)

    text = str(_ocr_field(source, "text", "") or "")
    x_pct = number("x_pct", _ocr_field(default_block, "x_pct", 0.05))
    max_x_pct = number("max_x_pct", _ocr_field(default_block, "max_x_pct", 0.95))
    y_pct = number("y_pct", _ocr_field(default_block, "y_pct", 0.75))
    max_y_pct = number("max_y_pct", _ocr_field(default_block, "max_y_pct", 0.82))
    if not (0.0 <= x_pct < max_x_pct <= 1.0):
        x_pct, max_x_pct = 0.05, 0.95
    if not (0.0 <= y_pct < max_y_pct <= 1.0):
        y_pct, max_y_pct = 0.75, 0.82
    return OCRBlock(
        text=text,
        start=float(start_t),
        end=float(end_t),
        x_pct=x_pct,
        max_x_pct=max_x_pct,
        y_pct=y_pct,
        max_y_pct=max_y_pct,
        prob=number("prob", 0.85),
        sample_segment_id=_ocr_field(source, "sample_segment_id"),
        sample_time=number("sample_time", start_t),
        is_subtitle=True,
        in_subtitle_band=True,
        is_packaging=False,
        is_static=False,
        type="subtitle",
    )


def _subtitle_from_recovery(rec, index, visual_mode):
    start_t = float(rec["start"])
    end_t = float(rec["end"])
    text = str(rec.get("text", "") or "").strip()
    best = rec.get("best_block")
    if isinstance(best, OCRBlock):
        best_block = copy.copy(best)
        best_block.start = start_t
        best_block.end = end_t
    else:
        best_block = _tracking_ocr_block(best or {}, start_t, end_t, {})
        best_block.text = text
    best_block.is_subtitle = True
    best_block.in_subtitle_band = True
    best_block.is_packaging = False
    best_block.is_static = False
    best_block.type = "subtitle"
    best_block.geometry_verified = bool(visual_mode) or bool(
        _ocr_field(best_block, "geometry_verified", False)
    )

    tracking = []
    for block in rec.get("tracking_blocks") or []:
        left = float(_ocr_field(block, "start", start_t) or start_t)
        right = float(_ocr_field(block, "end", end_t) or end_t)
        left = max(start_t, min(left, end_t))
        right = max(left, min(right, end_t))
        if right <= left:
            continue
        tracking.append(_tracking_ocr_block(block, left, right, best_block))
    if not tracking:
        tracking = [_tracking_ocr_block(best_block, start_t, end_t, best_block)]
    for block in tracking:
        block.geometry_verified = bool(visual_mode) or bool(
            _ocr_field(block, "geometry_verified", False)
        )
        block.visual_locked = bool(visual_mode)

    new_seg = srt.Subtitle(
        index=index,
        start=timedelta(seconds=start_t),
        end=timedelta(seconds=end_t),
        content=text,
    )
    new_seg.best_block = best_block
    new_seg.tracking_blocks = tracking
    new_seg.y_pct = best_block.y_pct
    new_seg.max_y_pct = best_block.max_y_pct
    new_seg.ocr_mask_status = "located"
    new_seg.geometry_verified = bool(visual_mode)
    new_seg.visual_sub_to_speech = bool(visual_mode)
    new_seg.visual_start_sec = float(rec.get("visual_start_sec", start_t))
    new_seg.visual_end_sec = float(rec.get("visual_end_sec", end_t))
    new_seg.is_subtitle = True
    new_seg.in_subtitle_band = True
    new_seg.is_packaging = False
    return new_seg


def _legacy_subtitle_from_recovery(rec, index):
    """Preserve the pre-visual recovery shape when the opt-in flag is off."""
    start_t = float(rec["start"])
    end_t = float(rec["end"])
    best = rec["best_block"]

    def field(name, default):
        return _ocr_field(best, name, default)

    best_block = OCRBlock(
        text=str(field("text", "") or ""),
        start=start_t,
        end=end_t,
        x_pct=float(field("x_pct", 0.0)),
        max_x_pct=float(field("max_x_pct", 1.0)),
        y_pct=float(field("y_pct", 0.0)),
        max_y_pct=float(field("max_y_pct", 1.0)),
        prob=float(field("prob", 1.0)),
    )
    new_seg = srt.Subtitle(
        index=index,
        start=timedelta(seconds=start_t),
        end=timedelta(seconds=end_t),
        content=str(rec["text"]),
    )
    new_seg.best_block = best_block
    new_seg.y_pct = best_block.y_pct
    new_seg.max_y_pct = best_block.max_y_pct
    new_seg.tracking_blocks = []
    new_seg.ocr_mask_status = "located"
    new_seg.geometry_verified = False
    return new_seg


def _perform_visual_subtitle_ocr(cap, width, height, duration, existing_segments, stopped, metrics=None):
    """Recover burned-in captions without relying on an ASR transcript."""
    if metrics is None:
        metrics = {}
    times = _visual_grid_times(existing_segments, duration, step=0.35)
    if not times:
        return [], 0.85

    all_blocks = []
    previous_roi = None
    previous_rows = None
    previous_time = None
    previous_scale = 1.0
    previous_offset = 0
    roi_y0, roi_y1 = int(0.35 * height), int(0.95 * height)
    roi_x0, roi_x1 = int(0.08 * width), int(0.92 * width)

    def append_rows(rows, sample_time, scale_ratio, crop_offset):
        for mapped in normalized_rows(rows, scale_ratio, crop_offset, width, height):
            block = OCRBlock(
                **mapped,
                start=sample_time - 1.2,
                end=sample_time + 1.2,
                sample_segment_id=0,
                sample_time=sample_time,
            )
            block.sample_in_cue = True
            all_blocks.append(block)

    logger.info("Visual OCR-to-TTS: scanning %d dense timeline samples (step=0.35s)", len(times))
    for position, current_time in enumerate(times):
        if stopped():
            raise RuntimeError("OCR stop requested")
        cap.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000)
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, current_time - 0.2) * 1000)
            ok, frame = cap.read()
        if not ok:
            logger.warning("Could not decode visual OCR frame at %.2fs", current_time)
            continue

        roi = frame[roi_y0:roi_y1, roi_x0:roi_x1]
        roi_small = None
        if roi.size:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            roi_small = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)

        can_reuse = (
            previous_rows is not None
            and previous_roi is not None
            and roi_small is not None
            and previous_time is not None
            and 0.0 <= current_time - previous_time <= 1.0
        )
        if can_reuse:
            mad = float(np.mean(cv2.absdiff(roi_small, previous_roi)))
            if mad < 1.8:
                append_rows(previous_rows, current_time, previous_scale, previous_offset)
                metrics["visual_roi_reused"] = metrics.get("visual_roi_reused", 0) + 1
                previous_time = current_time
                previous_roi = roi_small
                continue

        scale_ratio = min(1.0, 960.0 / max(width, 1))
        if scale_ratio < 1.0:
            proc_frame = cv2.resize(
                frame,
                (max(1, round(width * scale_ratio)), max(1, round(height * scale_ratio))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            proc_frame = frame
        results = _readtext_batch([proc_frame])
        rows = results[0] if results else []
        has_valid_rows = any(len(row) >= 2 and str(row[1] or "").strip() for row in rows)
        previous_rows = rows if has_valid_rows else None
        previous_time = current_time
        previous_scale = scale_ratio
        previous_offset = 0
        previous_roi = roi_small
        metrics["visual_ocr_calls"] = metrics.get("visual_ocr_calls", 0) + 1
        append_rows(rows, current_time, scale_ratio, 0)
        if (position + 1) % 100 == 0:
            logger.info(
                "Visual OCR progress: %d/%d samples, GPU calls=%d, reused=%d",
                position + 1, len(times),
                metrics.get("visual_ocr_calls", 0), metrics.get("visual_roi_reused", 0),
            )

    if not all_blocks:
        return [], 0.85
    band = select_chinese_subtitle_band(
        blocks=all_blocks,
        segment_texts={},
        frame_width=width,
        frame_height=height,
        allow_visual_only=True,
        duration=duration,
    )
    if getattr(band, "support", 0) <= 0 or getattr(band, "mode", "") == "default":
        logger.info("Visual OCR-to-TTS: no dynamic Chinese subtitle band found")
        return [], 0.85

    recovered = recover_missing_subtitles_from_ocr(
        all_blocks=all_blocks,
        srt_segments=existing_segments or [],
        band=band,
        duration=duration,
        visual_sub_to_speech=True,
    )
    segments = [
        _subtitle_from_recovery(rec, index=position + 1, visual_mode=True)
        for position, rec in enumerate(recovered)
    ]
    logger.info("Visual OCR-to-TTS recovered %d subtitle cues", len(segments))
    return segments, float(getattr(band, "top", 0.85) or 0.85)


def _visual_segment_center_y(seg):
    blocks = list(getattr(seg, "tracking_blocks", None) or [])
    if not blocks and getattr(seg, "best_block", None) is not None:
        blocks = [seg.best_block]
    centers = []
    for block in blocks:
        try:
            y1 = float(_ocr_field(block, "y_pct", 0.0) or 0.0)
            y2 = float(_ocr_field(block, "max_y_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if y2 > y1:
            centers.append((y1 + y2) * 0.5)
    if centers:
        return sum(centers) / len(centers)
    return float(getattr(seg, "y_pct", 0.85) or 0.85)


def normalize_visual_speech_timing(segments, duration=None):
    """Merge micro-captions and extend speech time, not visual cover time."""
    visual = [seg for seg in segments if getattr(seg, "visual_sub_to_speech", False)]
    if not visual:
        return segments

    kept = []
    removed = set()
    ordered_all = sorted(segments, key=lambda item: item.start)
    for position, seg in enumerate(ordered_all):
        if not getattr(seg, "visual_sub_to_speech", False) or position == 0:
            kept.append(seg)
            continue
        previous = ordered_all[position - 1]
        if id(previous) in removed:
            previous = next(
                (item for item in reversed(ordered_all[:position]) if id(item) not in removed),
                None,
            )
        if (previous is not None and getattr(previous, "visual_sub_to_speech", False)):
            gap = (seg.start - previous.end).total_seconds()
            previous_duration = (previous.end - previous.start).total_seconds()
            total_chars = _visual_chinese_count(previous.content) + _visual_chinese_count(seg.content)
            same_band = abs(_visual_segment_center_y(previous) - _visual_segment_center_y(seg)) < 0.035
            if -0.5 < gap < 0.25 and same_band and total_chars <= 14 and previous_duration < 0.85:
                left_text = str(previous.content or "").rstrip("，, ")
                right_text = str(seg.content or "").lstrip("，, ")
                previous.content = f"{left_text}，{right_text}"
                previous.end = max(previous.end, seg.end)
                previous.tracking_blocks = sorted(
                    list(getattr(previous, "tracking_blocks", None) or [])
                    + list(getattr(seg, "tracking_blocks", None) or []),
                    key=lambda block: float(_ocr_field(block, "start", 0.0) or 0.0),
                )
                previous.visual_start_sec = min(
                    float(getattr(previous, "visual_start_sec", previous.start.total_seconds())),
                    float(getattr(seg, "visual_start_sec", seg.start.total_seconds())),
                )
                previous.visual_end_sec = max(
                    float(getattr(previous, "visual_end_sec", previous.end.total_seconds())),
                    float(getattr(seg, "visual_end_sec", seg.end.total_seconds())),
                )
                if getattr(previous, "best_block", None) is not None:
                    previous.best_block.end = previous.visual_end_sec
                removed.add(id(seg))
                continue
        kept.append(seg)

    if removed:
        segments[:] = [seg for seg in segments if id(seg) not in removed]

    ordered = sorted(segments, key=lambda item: item.start)
    for position, seg in enumerate(ordered):
        if not getattr(seg, "visual_sub_to_speech", False):
            continue
        current_duration = (seg.end - seg.start).total_seconds()
        min_speech_duration = max(1.10, _visual_chinese_count(seg.content) * 0.18)
        next_start = (
            ordered[position + 1].start.total_seconds()
            if position + 1 < len(ordered)
            else float(duration or seg.end.total_seconds())
        )
        gap = next_start - seg.end.total_seconds()
        if current_duration >= min_speech_duration or gap <= 0.12:
            continue
        target_end = min(seg.start.total_seconds() + min_speech_duration, next_start - 0.08)
        if duration:
            target_end = min(target_end, float(duration))
        if target_end > seg.end.total_seconds():
            seg.end = timedelta(seconds=target_end)
            seg.speech_end_sec = target_end
    return segments


@stage("ocr", cleanup=release_ocr_reader)
@ocr_proxy
def perform_video_ocr(video_path, target_lang='vi', sample_rate=1.0, api_key=None, srt_segments=None, visual_sub_to_speech=False, **kwargs):
    logger.info(f"Bắt đầu OCR toàn diện trên video {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError('OCR cannot open video; mask coordinates not verified')

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps < 1: fps = 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count_key = getattr(cv2, "CAP_PROP_FRAME_COUNT", None)
    # Frame count / nominal FPS underestimates the timeline of VFR inputs.
    duration = float(kwargs.get("_source_duration", 0) or 0)
    if not math.isfinite(duration) or duration <= 0:
        duration = cap.get(frame_count_key) / fps if frame_count_key is not None else 0
    if width <= 0 or height <= 0:
        cap.release()
        raise ValueError("Video has invalid dimensions")

    all_blocks = []

    def segment_id(seg, ordinal):
        sid = getattr(seg, 'index', None)
        return sid if sid is not None else ordinal

    def stopped():
        try:
            from . import shared_state
            from .batch_control import stop_check
        except (ImportError, ValueError):
            try:
                import shared_state
                from batch_control import stop_check
            except (ImportError, ValueError):
                shared_state = None
                stop_check = None
        predicate = stop_check.get() if stop_check else None
        return bool(shared_state and getattr(shared_state, 'stop_requested', False)) or bool(predicate and predicate())

    speech_coverage = 0.0
    timeline_duration = float(duration)
    if not timeline_duration:
        try:
            timeline_duration = max(
                seg.end.total_seconds() for seg in srt_segments or []
            )
        except Exception:
            pass
        if not timeline_duration:
            try:
                frame_count_key = getattr(cv2, "CAP_PROP_FRAME_COUNT", None)
                if frame_count_key is not None:
                    timeline_duration = cap.get(frame_count_key) / fps
            except Exception:
                pass
    if srt_segments:
        for seg in srt_segments:
            try:
                seg_start = seg.start.total_seconds()
                seg_end = min(timeline_duration, seg.end.total_seconds()) if timeline_duration else seg.end.total_seconds()
                if seg_end > seg_start:
                    speech_coverage += max(0.0, seg_end - seg_start)
            except Exception:
                continue

    visual_mode = (
        bool(visual_sub_to_speech)
        and (not srt_segments or len(srt_segments) == 0 or (timeline_duration > 0 and speech_coverage < 0.25 * timeline_duration))
    )

    if visual_mode:
        logger.info("Visual OCR-to-TTS mode active: no speech or speech coverage %.2f%%", 100 * speech_coverage / timeline_duration if timeline_duration else 0.0)
        try:
            visual_segments, visual_main_y = _perform_visual_subtitle_ocr(
                cap, width, height, timeline_duration, srt_segments, stopped, metrics=kwargs.get('metrics')
            )
        finally:
            cap.release()
        if not visual_segments:
            logger.warning("Visual OCR-to-TTS completed but found zero dynamic captions")
            return [], width, height, 0.85
        combined = list(srt_segments or []) + visual_segments
        combined.sort(key=lambda item: item.start)
        normalize_visual_speech_timing(combined, duration=timeline_duration)
        combined.sort(key=lambda item: item.start)
        for idx, seg in enumerate(combined, start=1):
            seg.index = idx
        logger.info("Visual OCR-to-TTS pipeline complete: %d cues recovered", len(visual_segments))
        return combined, width, height, visual_main_y

    # === TỐI ƯU HÓA SIÊU TỐC OCR THEO TỪNG ĐOẠN THOẠI (SMART SKIP OCR) ===
    short_thresh = float(os.getenv("V1_SHORT_MAX_SECONDS", "420.0"))
    effective_duration = duration
    if effective_duration <= 0 and srt_segments:
        try:
            effective_duration = max(s.end.total_seconds() for s in srt_segments)
        except Exception:
            pass

    if effective_duration > 0 and effective_duration <= short_thresh:
        ocr_strategy = "full"
        logger.info("Chế độ OCR cho video ngắn (%.1fs <= %.0fs): 'full' (đã tắt smart_skip)", effective_duration, short_thresh)
    else:
        ocr_strategy = kwargs.get("ocr_strategy")
        if not ocr_strategy or ocr_strategy == "auto":
            try:
                try:
                    from backend.job_config_service import resolve_job_frozen_config
                except ImportError:
                    from job_config_service import resolve_job_frozen_config
                frozen = resolve_job_frozen_config(video_path)
                if frozen and "effective_config" in frozen and "planned_pipeline" in frozen["effective_config"]:
                    ocr_strategy = frozen["effective_config"]["planned_pipeline"].get("ocr_strategy")
                elif frozen and "routing" in frozen and frozen["routing"]:
                    ocr_strategy = frozen["routing"].get("planned_pipeline", {}).get("ocr_strategy")
            except Exception:
                pass

        if not ocr_strategy or ocr_strategy == "auto":
            try:
                from .v1_feature_flags import get_feature_flags
            except ImportError:
                try:
                    from v1_feature_flags import get_feature_flags
                except ImportError:
                    get_feature_flags = lambda: {}
            flags = get_feature_flags()
            enable_smart = flags.get("V1_SMART_SKIP_OCR")
            if enable_smart is None:
                enable_smart = os.getenv("V1_SMART_SKIP_OCR", os.getenv("ENABLE_SMART_SKIP_OCR", "true")).strip().lower() in ("1", "true", "yes", "on")
            else:
                enable_smart = bool(enable_smart)

            if effective_duration > short_thresh and enable_smart:
                ocr_strategy = "smart_skip"
            else:
                ocr_strategy = "full"
    
    logger.info("Chế độ OCR đã chọn: '%s' (effective_duration=%.1fs, caller=%s)", ocr_strategy, effective_duration, kwargs.get("ocr_strategy"))

    target_timestamps = []
    if "adaptive" in kwargs:
        is_adaptive = bool(kwargs["adaptive"])
    else:
        is_adaptive = (ocr_strategy != "smart_skip")
    metrics = kwargs.get('metrics')
    if metrics is None:
        metrics = {}
    interval = max(0.08, min(0.5, float(os.getenv("OCR_TRACK_INTERVAL", "0.2"))))

    if srt_segments:
        # Cover every speech segment; process recognition in bounded batches.
        interval = max(0.1, min(5.0, float(os.getenv("V1_OCR_TRACK_INTERVAL", "0.55"))))
        max_samples_per_cue = max(1, int(os.getenv("V1_OCR_MAX_SAMPLES_PER_SEGMENT", "8")))
        total_segs = len(srt_segments)
        for seg_idx, seg in enumerate(srt_segments):
            s = seg.start.total_seconds()
            e = seg.end.total_seconds()
            if e <= s:
                continue

            if duration > 0:
                e = min(e, duration)
            cue_dur = max(0.1, e - s)
            # Smart mode saves extra samples, NEVER the only geometry sample.
            if ocr_strategy == 'smart_skip':
                count = 1
            elif cue_dur <= 2.05:
                count = min(max_samples_per_cue, max(1, math.ceil(cue_dur / 1.2)))
            else:
                count = min(max_samples_per_cue, max(3, math.ceil(cue_dur / interval)))
            for t in sample_times(s, e, count):
                target_timestamps.append((t, seg, seg_idx))
        if ocr_strategy == 'smart_skip':
            logger.info('Smart OCR: one mandatory full-frame geometry sample per cue (%d cues)', total_segs)
    else:
        # Without a transcript there is no reliable way to distinguish scene text.
        cap.release()
        return [], width, height, 0.85

    segment_texts = {}
    if srt_segments:
        for idx, seg in enumerate(srt_segments):
            s_id = getattr(seg, 'index', None)
            s_key = s_id if s_id is not None else idx
            segment_texts[s_key] = str(getattr(seg, 'content', '') or '').strip()

        # A short ASR interjection may be printed with the next full sentence.
        # Only borrow immediate contiguous speech; never seed from arbitrary scene text.
        for idx, seg in enumerate(srt_segments):
            key = getattr(seg, "index", None)
            key = key if key is not None else idx
            own = segment_texts[key]
            if len("".join(c for c in own if "\u3400" <= c <= "\u9fff")) <= 4:
                context = [own]
                if idx and (seg.start - srt_segments[idx - 1].end).total_seconds() <= .3:
                    context.insert(0, str(srt_segments[idx - 1].content))
                if idx + 1 < len(srt_segments) and (srt_segments[idx + 1].start - seg.end).total_seconds() <= .3:
                    context.append(str(srt_segments[idx + 1].content))
                segment_texts[key] = " ".join(context)

    crop_y_start, crop_y_end = 0, height
    captured_frames = []
    recognized_samples = []
    early_band_checked = False
    early_band_locked = False
    sampled_seg_indices = set()

    def flush_frames():
        nonlocal crop_y_start, crop_y_end, early_band_checked, early_band_locked
        if not captured_frames:
            return
        if stopped():
            raise RuntimeError('OCR stop requested')
        results = _readtext_batch([item[2] for item in captured_frames])
        if len(results) != len(captured_frames):
            raise RuntimeError("OCR returned incomplete frame results")
        for item, rows in zip(captured_frames, results):
            current_time, scale_ratio, _, target_seg, seg_idx, crop_offset = item
            recognized_samples.append((current_time, scale_ratio, target_seg, seg_idx, rows, crop_offset))
        captured_frames.clear()

        # Early Subtitle Band Lock: khi video có >= 16 câu thoại ở chế độ 'full'
        # và sau 24 frame đầu tiên đã xác lập được dải phụ đề cố định độ tin cậy cao,
        # khóa ROI theo trục dọc và chỉ lấy 1 mẫu/câu cho phần còn lại để tăng tốc 2.5x.
        if (
            not early_band_checked
            and not is_adaptive
            and ocr_strategy == "full"
            and len(srt_segments) >= 16
            and len(recognized_samples) >= 24
        ):
            early_band_checked = True
            try:
                early_blocks = []
                for c_time, s_ratio, t_seg, s_idx, r_rows, c_off in recognized_samples:
                    s_id = segment_id(t_seg, s_idx)
                    for mb in normalized_rows(r_rows, s_ratio, c_off, width, height):
                        eb = OCRBlock(
                            **mb,
                            start=c_time - 1.2,
                            end=c_time + 1.2,
                            sample_segment_id=s_id,
                            sample_time=c_time,
                        )
                        eb.sample_in_cue = (
                            t_seg.start.total_seconds() <= c_time < t_seg.end.total_seconds()
                        )
                        early_blocks.append(eb)
                early_band = select_chinese_subtitle_band(
                    blocks=early_blocks,
                    segment_texts=segment_texts,
                    frame_width=width,
                    frame_height=height,
                    default_top=0.75,
                    default_bottom=0.82,
                )
                if (
                    getattr(early_band, "mode", "") == "asr_match"
                    and getattr(early_band, "support", 0) >= 10
                    and (float(getattr(early_band, "bottom", 1.0)) - float(getattr(early_band, "top", 0.0))) <= 0.10
                ):
                    early_band_locked = True
                    crop_y_start = max(0, int((float(early_band.top) - 0.18) * height))
                    crop_y_end = min(height, math.ceil((float(early_band.bottom) + 0.18) * height))
                    logger.info(
                        "⚡ Early Subtitle Band Lock kích hoạt sau %d frame (support=%d, Y=%.3f..%.3f) -> ROI [%d:%d], 1 sample/cue cho phần còn lại",
                        len(recognized_samples),
                        early_band.support,
                        early_band.top,
                        early_band.bottom,
                        crop_y_start,
                        crop_y_end,
                    )
            except Exception as eb_err:
                logger.debug("Early Subtitle Band Lock check skipped: %s", eb_err)

    if is_adaptive:
        try:
            from .ocr_adaptive import collect_adaptive_samples
        except ImportError:
            from ocr_adaptive import collect_adaptive_samples
        samples = collect_adaptive_samples(cap, srt_segments, width, height,
                                          fps, duration, _readtext_batch, stopped, metrics)
        recognized_samples.extend((t, ratio, seg, idx, holder['rows'], 0)
                                  for t, ratio, seg, idx, holder in samples)
        target_timestamps = []

    try:
        for current_time, target_seg, seg_idx in target_timestamps:
            if stopped():
                cap.release()
                raise RuntimeError('OCR stop requested')

            if early_band_locked and seg_idx in sampled_seg_indices:
                continue

            cap.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000)
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, (current_time - 0.2)) * 1000)
                ret, frame = cap.read()
            if not ret:
                logger.warning('Could not decode OCR verification frame at %.2fs, skipping frame sample', current_time)
                continue

            sampled_seg_indices.add(seg_idx)

            # Include both edges; bottom captions used to be clipped before OCR.
            cropped_frame = frame[crop_y_start:crop_y_end, :]

            # Bounded GPU input; higher-resolution rescue only for missing cues.
            orig_crop_h, orig_crop_w = cropped_frame.shape[:2]
            scale_ratio = 1.0
            if orig_crop_w > 960:
                scale_ratio = 960.0 / orig_crop_w
                target_w = 960
                target_h = int(orig_crop_h * scale_ratio)
                proc_frame = cv2.resize(cropped_frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
            else:
                proc_frame = cropped_frame

            captured_frames.append((current_time, scale_ratio, proc_frame, target_seg, seg_idx, crop_y_start))
            if len(captured_frames) >= 12:
                flush_frames()

    finally:
        cap.release()
    flush_frames()
    if target_timestamps and not captured_frames and not recognized_samples:
        raise RuntimeError("Could not decode any video frames for OCR verification")
    def append_recognized(samples):
        for current_time, scale_ratio, target_seg, seg_idx, results, crop_offset in samples:
            sample_id = segment_id(target_seg, seg_idx)
            for mb in normalized_rows(results, scale_ratio, crop_offset, width, height):
                block = OCRBlock(**mb, start=current_time - 1.2,
                                 end=current_time + 1.2,
                                 sample_segment_id=sample_id,
                                 sample_time=current_time)
                block.sample_in_cue = (target_seg.start.total_seconds() <= current_time
                                       < target_seg.end.total_seconds())
                all_blocks.append(block)

    append_recognized(recognized_samples)
    observation_times = {}
    for item in recognized_samples:
        observation_times.setdefault(segment_id(item[2], item[3]), set()).add(item[0])
    sampled_ids = set(observation_times)
    recognized_samples.clear()

    band = select_chinese_subtitle_band(
        blocks=all_blocks,
        segment_texts=segment_texts,
        frame_width=width,
        frame_height=height,
        default_top=0.75,
        default_bottom=0.82,
    )

    # Rescue only missing coordinates: two in-cue samples, higher resolution,
    # established subtitle ROI (or full frame if no reliable track exists).
    # Nếu dải phụ đề cố định đã khớp rất chắc chắn (asr_match, support >= 15, độ dày dải <= 10%)
    # và số câu khuyết ít (<= 25%), bỏ qua bước tua video cắt frame cứu hộ vì Global Subtitle Band
    # ở bước kế tiếp sẽ tự động gán chính xác tọa độ dải phụ đề chung cho các câu này.
    missing = [(i, seg) for i, seg in enumerate(srt_segments)
               if segment_id(seg, i) not in band.selected_by_segment]
    high_conf_band_locked = (
        getattr(band, "mode", "") == "asr_match"
        and getattr(band, "support", 0) >= 15
        and (float(getattr(band, "bottom", 1.0)) - float(getattr(band, "top", 0.0))) <= 0.10
        and len(srt_segments) >= 12
        and len(missing) <= max(3, int(len(srt_segments) * 0.25))
    )
    if missing and not high_conf_band_locked and not (ocr_strategy == "full" and band.support == 0 and len(srt_segments) >= 3):
        logger.info('OCR geometry rescue: %d/%d cues, GPU only', len(missing), len(srt_segments))
        rescue_cap = cv2.VideoCapture(video_path)
        if not rescue_cap.isOpened():
            rescue_cap.release()
            raise RuntimeError('OCR rescue cannot open video')
        roi_start = max(0, int((band.top - .08) * height)) if band.support else 0
        roi_end = min(height, math.ceil((band.bottom + .08) * height)) if band.support else height
        try:
            for seg_idx, target_seg in missing:
                if stopped():
                    raise RuntimeError('OCR stop requested')
                end = target_seg.end.total_seconds()
                if duration > 0:
                    end = min(end, duration)
                for current_time in rescue_times(target_seg.start.total_seconds(), end):
                    rescue_cap.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000)
                    ok, frame = rescue_cap.read()
                    if not ok:
                        rescue_cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, (current_time - 0.2)) * 1000)
                        ok, frame = rescue_cap.read()
                    if not ok:
                        logger.warning('Could not decode OCR rescue frame at %.2fs, skipping frame sample', current_time)
                        continue
                    crop = frame[roi_start:roi_end, :]
                    scale_ratio = min(1., 1280. / width)
                    proc = cv2.resize(crop, (round(width * scale_ratio), max(1, round((roi_end-roi_start) * scale_ratio))), interpolation=cv2.INTER_AREA) if scale_ratio < 1 else crop
                    captured_frames.append((current_time, scale_ratio, proc, target_seg, seg_idx, roi_start))
                    if len(captured_frames) >= 12:
                        flush_frames()
            flush_frames()
        finally:
            rescue_cap.release()
        for item in recognized_samples:
            observation_times.setdefault(segment_id(item[2], item[3]), set()).add(item[0])
        sampled_ids.update(observation_times)
        append_recognized(recognized_samples)
        band = select_chinese_subtitle_band(all_blocks, segment_texts, width, height)

    for idx, seg in enumerate(srt_segments):
        sid = segment_id(seg, idx)
        seg.best_block = None
        seg.tracking_blocks = []
        seg.ocr_mask_status = ('located' if sid in band.selected_by_segment else
                               'unresolved' if sid not in sampled_ids or has_caption_evidence(all_blocks, sid, band)
                               else 'no_caption_evidence')

    if band.support > 0 and band.mode != "default":
        global_med_top = band.top
        global_med_bottom = band.bottom
        main_y_pct = global_med_top
        logger.info(f"🎯 Global Subtitle Band detected ({band.mode}, support={band.support}): Top={global_med_top:.3f}, Bottom={global_med_bottom:.3f}")

        if srt_segments:
            for idx, seg in enumerate(srt_segments):
                s_id = getattr(seg, 'index', None)
                s_key = s_id if s_id is not None else idx
                b = band.selected_by_segment.get(s_key)

                seg_s = max(0.0, seg.start.total_seconds() - 0.3)
                next_start = (srt_segments[idx + 1].start.total_seconds()
                              if idx + 1 < len(srt_segments) else duration)
                seg_e = min(seg.end.total_seconds() + 0.4, next_start) if next_start > seg.end.total_seconds() else (seg.end.total_seconds() + 0.3)
                if duration > 0:
                    seg_e = min(seg_e, duration)

                if b:
                    selected = stabilize_samples(band.selected_by_sample.get(s_key, []))
                    tracking = []
                    observed = sorted(observation_times.get(s_key, []))
                    selected_times = {row['sample_time'] for row in selected}
                    verified_absence = [t for t in observed if t not in selected_times and
                                        not has_caption_evidence(
                                            [block for block in all_blocks
                                             if block.sample_segment_id == s_key
                                             and abs(block.sample_time-t) < .00001], s_key, band)]
                    for position, row in enumerate(selected):
                        left = seg_s if position == 0 else (selected[position - 1]["sample_time"] + row["sample_time"]) / 2.0
                        right = seg_e if position == len(selected) - 1 else (row["sample_time"] + selected[position + 1]["sample_time"]) / 2.0
                        if is_adaptive:
                            # Bound each observation by confirmed empty frames;
                            # one missed sample is tolerated, sustained absence is not.
                            before = [t for t in verified_absence if left <= t < row['sample_time']]
                            after = [t for t in verified_absence if row['sample_time'] < t <= right]
                            if len(before) >= 2 or (position == 0 and before):
                                left = (before[-1] + row['sample_time']) / 2.
                            if len(after) >= 2 or (position == len(selected)-1 and after):
                                right = (row['sample_time'] + after[0]) / 2.
                        if right > left:
                            tracking.append(OCRBlock(
                                **{key: row[key] for key in
                                   ("text", "x_pct", "max_x_pct", "y_pct", "max_y_pct")},
                                start=max(seg_s, left), end=min(seg_e, right),
                                prob=row.get("prob", 0.0),
                                sample_segment_id=s_key, sample_time=row["sample_time"],
                                is_subtitle=True, in_subtitle_band=True,
                                is_packaging=False, type='subtitle',
                            ))
                    seg.tracking_blocks = tracking

                    seg.best_block = OCRBlock(
                        text=b["text"],
                        start=seg_s,
                        end=seg_e,
                        x_pct=b["x_pct"],
                        max_x_pct=b["max_x_pct"],
                        y_pct=b["y_pct"],
                        max_y_pct=b["max_y_pct"],
                        prob=b.get("prob", 1.0),
                        is_subtitle=True, in_subtitle_band=True,
                        is_packaging=False, type='subtitle',
                    )
                    seg.is_subtitle = True
                    seg.in_subtitle_band = True
                    seg.is_packaging = False
                    seg.geometry_verified = is_adaptive
                    for track in tracking:
                        track.geometry_verified = is_adaptive
                    seg.y_pct = b["y_pct"]
                    seg.max_y_pct = b["max_y_pct"]
                    logger.info(f"Sync (Subtitle Band): '{str(getattr(seg, 'content', ''))[:15]}' -> Y: {seg.y_pct:.3f} - {seg.max_y_pct:.3f}")
                else:
                    seg.y_pct = global_med_top
                    seg.max_y_pct = global_med_bottom
                    if getattr(band, 'support', 0) >= 2 or len(srt_segments) > 2:
                        seg.best_block = OCRBlock(
                            text=getattr(seg, 'content', ''),
                            start=seg_s,
                            end=seg_e,
                            x_pct=0.05,
                            max_x_pct=0.95,
                            y_pct=global_med_top,
                            max_y_pct=global_med_bottom,
                            prob=0.85,
                            is_subtitle=True,
                            in_subtitle_band=True,
                            is_packaging=False,
                            type='subtitle',
                        )
                        seg.is_subtitle = True
                        seg.in_subtitle_band = True
                        seg.is_packaging = False
                        seg.geometry_verified = False
                        seg.ocr_mask_status = 'located'
                        logger.info(f"Auto-repair (Global Subtitle Band fallback): cue #{s_key} -> located at Y: {seg.y_pct:.3f} - {seg.max_y_pct:.3f}")

        # BỔ SUNG CÂU THOẠI TỪ OCR (Nếu ASR bị nhạc to át mất câu)
        try:
                recovered_subs = recover_missing_subtitles_from_ocr(
                    all_blocks=all_blocks,
                    srt_segments=srt_segments,
                    band=band,
                    duration=duration,
                    visual_sub_to_speech=False,
                )
                if recovered_subs:
                    logger.info(f"🎯 OCR Fallback phát hiện {len(recovered_subs)} câu phụ đề bị ASR bỏ sót: {[r['text'] for r in recovered_subs]}")
                    segments = [
                        _legacy_subtitle_from_recovery(rec, index=len(srt_segments) + position + 1)
                        for position, rec in enumerate(recovered_subs)
                    ]
                    srt_segments.extend(segments)
                    srt_segments.sort(key=lambda s: s.start)
                    for idx, seg in enumerate(srt_segments, start=1):
                        seg.index = idx
        except Exception as ocr_rec_err:
            logger.warning(f"OCR subtitle recovery error: {ocr_rec_err}")
    else:
        logger.info("No reliable Chinese subtitle band detected (video without subtitles or only static packaging/logos).")
        main_y_pct = 0.85
        if srt_segments:
            for seg in srt_segments:
                seg.best_block = None
                seg.tracking_blocks = []
                seg.y_pct = 0.85
                seg.max_y_pct = 0.90
                if seg.ocr_mask_status != 'unresolved':
                    seg.ocr_mask_status = 'no_caption_evidence'

    report = geometry_summary(srt_segments)
    logger.info('OCR geometry audit: %s', report)
    if kwargs.get('geometry_report_path'):
        try:
            from .v1_stage_runtime import save_payload
        except ImportError:
            from v1_stage_runtime import save_payload
        details = {}
        if kwargs.get('include_observations'):
            details['observations'] = [vars(block) for block in all_blocks]
        save_payload(kwargs['geometry_report_path'], geometry=report, sampling=metrics, **details)
    validate_mask_geometry(srt_segments, strict=kwargs.get('strict_geometry'))
    return [], width, height, main_y_pct


def _recover_missing_subtitles_legacy(all_blocks, srt_segments, band, duration=None):
    """Pre-visual recovery path; must stay byte-for-byte equivalent in behaviour."""
    if not all_blocks or getattr(band, "support", 0) <= 0 or getattr(band, "mode", "") == "default":
        return []
    existing_intervals = []
    if srt_segments:
        for seg in srt_segments:
            existing_intervals.append((seg.start.total_seconds() - 0.5, seg.end.total_seconds() + 0.5))

    candidates = []
    for b in all_blocks:
        if (_ocr_field(b, 'is_packaging') is True or _ocr_field(b, 'is_static') is True
                or _ocr_field(b, 'is_subtitle') is False or _ocr_field(b, 'in_subtitle_band') is False
                or str(_ocr_field(b, 'type', '')).lower() in ('packaging', 'logo', 'watermark', 'background')):
            continue
        prob = float(_ocr_field(b, "prob", 1.0))
        text = str(_ocr_field(b, "text", "")).strip()
        y_pct = float(_ocr_field(b, "y_pct", 0.0))
        sample_time = float(_ocr_field(b, "sample_time", 0.0))
        if not (band.top - 0.035 <= y_pct <= band.bottom + 0.035):
            continue
        ch_chars = [c for c in text if "\u3400" <= c <= "\u9fff"]
        if prob < 0.60 or len(ch_chars) < 2:
            continue
        if any(st <= sample_time <= et for st, et in existing_intervals):
            continue
        candidates.append(b)

    if not candidates:
        return []

    def get_st(blk):
        return float(_ocr_field(blk, "sample_time", 0.0))

    candidates.sort(key=get_st)
    clusters = []
    current_cluster = [candidates[0]]
    for b in candidates[1:]:
        prev_time = get_st(current_cluster[-1])
        same_caption = SequenceMatcher(
            None, str(_ocr_field(current_cluster[0], "text", "") or ""),
            str(_ocr_field(b, "text", "") or ""), autojunk=False,
        ).ratio() >= 0.8
        if get_st(b) - prev_time <= 1.5 and same_caption:
            current_cluster.append(b)
        else:
            clusters.append(current_cluster)
            current_cluster = [b]
    if current_cluster:
        clusters.append(current_cluster)

    recovered = []
    for cluster in clusters:
        times = [get_st(b) for b in cluster]
        if len(set(times)) < 2:
            continue
        start_t = max(0.0, min(times) - 0.3)
        end_t = max(times) + 0.5
        if duration and duration > 0:
            end_t = min(end_t, duration)
        if end_t - start_t < 0.8 and len(cluster) < 2:
            continue
        best_block = max(
            cluster,
            key=lambda item: (float(_ocr_field(item, "prob", 0.0)), len(str(_ocr_field(item, "text", "")))),
        )
        text = str(_ocr_field(best_block, "text", "")).strip()
        if not text:
            continue
        recovered.append({
            "start": start_t,
            "end": end_t,
            "text": text,
            "best_block": best_block,
            "samples": cluster,
        })

    return recovered


def _is_static_packaging_or_watermark(cluster, duration=None):
    for block in cluster:
        if _visual_watermark_text(_ocr_field(block, "text", "")):
            return True
    times = [float(_ocr_field(block, "sample_time", 0.0) or 0.0) for block in cluster]
    if not times:
        return True
    if max(times) - min(times) > 8.5:
        return True
    if duration and duration >= 15.0:
        intervals = []
        for block, sample_time in zip(cluster, times):
            try:
                left = float(_ocr_field(block, "start", sample_time) or sample_time)
                right = float(_ocr_field(block, "end", sample_time) or sample_time)
            except (TypeError, ValueError):
                left = right = sample_time
            intervals.append((left, right))
        if _merged_span(intervals) > 0.32 * float(duration):
            return True
    return False


def _recover_missing_subtitles_visual(all_blocks, srt_segments, band, duration=None):
    existing_intervals = []
    if srt_segments:
        for seg in srt_segments:
            existing_intervals.append((seg.start.total_seconds() - 0.5, seg.end.total_seconds() + 0.5))

    candidates = []
    for b in all_blocks:
        if (_ocr_field(b, 'is_packaging') is True or _ocr_field(b, 'is_static') is True
                or _ocr_field(b, 'is_subtitle') is False or _ocr_field(b, 'in_subtitle_band') is False
                or str(_ocr_field(b, 'type', '')).lower() in ('packaging', 'logo', 'watermark', 'background')):
            continue
        prob = float(_ocr_field(b, "prob", 1.0))
        text = str(_ocr_field(b, "text", "")).strip()
        y_pct = float(_ocr_field(b, "y_pct", 0.0))
        max_y_pct = float(_ocr_field(b, "max_y_pct", 0.0) or 0.0)
        sample_time = float(_ocr_field(b, "sample_time", 0.0))
        x_pct = float(_ocr_field(b, "x_pct", 0.0) or 0.0)
        max_x_pct = float(_ocr_field(b, "max_x_pct", 1.0) or 1.0)
        # A two-line caption can straddle the band edge; keep any block that
        # vertically intersects it instead of requiring the top row inside.
        if not (max_y_pct >= band.top - 0.035 and y_pct <= band.bottom + 0.035):
            continue
        # Device/UI readouts sit in a narrow, off-center block; captions are
        # either wide or centered in the frame.
        if max_x_pct - x_pct < 0.14 and abs((x_pct + max_x_pct) * 0.5 - 0.5) > 0.15:
            continue
        if prob < 0.60 or _visual_chinese_count(text) < 2:
            continue
        if any(st <= sample_time <= et for st, et in existing_intervals):
            continue
        candidates.append(b)

    if not candidates:
        return []

    def get_st(blk):
        return float(_ocr_field(blk, "sample_time", 0.0))

    def block_width(blk):
        return max(0.0, float(_ocr_field(blk, "max_x_pct", 1.0) or 1.0)
                   - float(_ocr_field(blk, "x_pct", 0.0) or 0.0))

    def block_center_x(blk):
        return (float(_ocr_field(blk, "x_pct", 0.0) or 0.0)
                + float(_ocr_field(blk, "max_x_pct", 1.0) or 1.0)) * 0.5

    def caption_line_plausible(blk):
        width = block_width(blk)
        cjk = _visual_chinese_count(_ocr_field(blk, "text", ""))
        if width >= 0.16 and cjk >= 2:
            return True
        if width >= 0.10 and abs(block_center_x(blk) - 0.5) <= 0.05:
            return True
        return cjk >= 4 and width >= 0.12

    def frame_caption_blocks(blocks):
        # Appliance/UI labels sit on their own row above or below the caption
        # with a visible gap. Keep the widest caption row plus only the rows
        # stacked against it, so those labels never enter the cue text.
        ordered = sorted(blocks, key=lambda blk: (
            float(_ocr_field(blk, "y_pct", 0.0) or 0.0),
            float(_ocr_field(blk, "x_pct", 0.0) or 0.0),
        ))
        plausible = [blk for blk in ordered if caption_line_plausible(blk)]
        if not plausible:
            return []
        seed = max(
            plausible,
            key=lambda blk: (block_width(blk), _visual_chinese_count(_ocr_field(blk, "text", ""))),
        )
        group = [seed]
        changed = True
        while changed and len(group) < 2:
            changed = False
            group_top = min(float(_ocr_field(blk, "y_pct", 0.0) or 0.0) for blk in group)
            group_bottom = max(float(_ocr_field(blk, "max_y_pct", 0.0) or 0.0) for blk in group)
            for blk in plausible:
                if any(blk is member for member in group):
                    continue
                top = float(_ocr_field(blk, "y_pct", 0.0) or 0.0)
                bottom = float(_ocr_field(blk, "max_y_pct", 0.0) or 0.0)
                gap = max(0.0, group_top - bottom, top - group_bottom)
                if gap > 0.02:
                    continue
                group.append(blk)
                changed = True
                if len(group) >= 2:
                    break
        return group

    def frame_caption_text(group):
        visual_lines = []
        for blk in sorted(group, key=lambda item: (
            float(_ocr_field(item, "y_pct", 0.0) or 0.0),
            float(_ocr_field(item, "x_pct", 0.0) or 0.0),
        )):
            text = str(_ocr_field(blk, "text", "") or "").strip()
            if not text:
                continue
            center_y = (float(_ocr_field(blk, "y_pct", 0.0) or 0.0)
                        + float(_ocr_field(blk, "max_y_pct", 0.0) or 0.0)) * 0.5
            target = next(
                (line for line in visual_lines if abs(line["center"] - center_y) <= 0.025),
                None,
            )
            key = _visual_caption_key(text)
            if target is None:
                visual_lines.append({"center": center_y, "text": text, "seen": {key}})
            elif key not in target["seen"]:
                target["seen"].add(key)
                target["text"] += text
        return "\n".join(line["text"] for line in visual_lines if line["text"])

    # One sampled frame can hold two caption lines. Cluster whole frames, not
    # individual blocks, so lines never split one caption into micro-cues.
    by_sample = {}
    for b in candidates:
        by_sample.setdefault(round(get_st(b), 3), []).append(b)

    groups = []
    for sample_time in sorted(by_sample):
        captions = frame_caption_blocks(by_sample[sample_time])
        if not captions:
            continue
        text = frame_caption_text(captions)
        if not text:
            continue
        groups.append({"time": sample_time, "blocks": captions, "text": text,
                       "prob": sum(float(_ocr_field(b, "prob", 0.0) or 0.0)
                                   for b in captions) / len(captions)})

    def group_score(group):
        return (
            group["prob"],
            _visual_chinese_count(group["text"]),
            len(group["text"]),
        )

    clusters = []
    for group in groups:
        if not clusters:
            clusters.append([group])
            continue
        cluster = clusters[-1]
        previous = cluster[-1]
        best_text = max(cluster, key=group_score)["text"]
        same_caption = (
            _same_visual_caption(best_text, group["text"], threshold=0.72)
            or _same_visual_caption(previous["text"], group["text"], threshold=0.72)
        )
        if group["time"] - previous["time"] <= 0.85 and same_caption:
            cluster.append(group)
        else:
            clusters.append([group])

    recovered = []
    for cluster in clusters:
        cluster_blocks = [b for group in cluster for b in group["blocks"]]
        if _is_static_packaging_or_watermark(cluster_blocks, duration):
            logger.info(
                "Visual OCR: dropped static/watermark caption %r",
                str(cluster[0]["text"])[:24],
            )
            continue
        times = [group["time"] for group in cluster]
        start_t = max(0.0, min(times) - 0.12)
        end_t = max(times) + 0.15
        if duration and duration > 0:
            end_t = min(end_t, duration)
        if end_t <= start_t:
            continue
        best_group = max(cluster, key=group_score)
        text = best_group["text"].strip()
        if not text:
            continue
        best_block = max(
            best_group["blocks"],
            key=lambda item: (
                _visual_chinese_count(_ocr_field(item, "text", "")),
                float(_ocr_field(item, "prob", 0.0)),
                len(str(_ocr_field(item, "text", "") or "")),
            ),
        )
        tracking = _tracking_blocks_for_cluster(cluster_blocks, start_t, end_t, band, visual_mode=True)
        if not tracking:
            tracking = [_tracking_ocr_block(best_block, start_t, end_t, best_block)]
        recovered.append({
            "start": start_t,
            "end": end_t,
            "text": text,
            "best_block": best_block,
            "samples": cluster_blocks,
            "tracking_blocks": tracking,
            "visual_start_sec": start_t,
            "visual_end_sec": end_t,
        })

    return recovered


def recover_missing_subtitles_from_ocr(all_blocks, srt_segments, band, duration=None, visual_sub_to_speech=False):
    """
    Recover missing speech segments from reliable OCR blocks in the Chinese Subtitle Band
    that were missed by Whisper ASR (e.g. drowned out by loud music).
    When visual_sub_to_speech=True, use denser clustering rules for burned-in captions.
    """
    if not visual_sub_to_speech:
        return _recover_missing_subtitles_legacy(all_blocks, srt_segments, band, duration)
    if not all_blocks or getattr(band, "support", 0) <= 0 or getattr(band, "mode", "") == "default":
        return []
    return _recover_missing_subtitles_visual(all_blocks, srt_segments, band, duration)
