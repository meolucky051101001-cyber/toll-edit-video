"""
v1_quality_gate.py - Quality Gate tự động 8 nhóm kiểm tra (Tool V1 - Giai đoạn 5).
Tự động xuất bản báo cáo QC sau khi xuất video thành phẩm.
Chính sách: BLOCK (mặc định) / WARN / REPORT_ONLY.
Lỗi toàn vẹn nghiêm trọng luôn bị chặn trước khi giao video, kể cả ở chế độ chỉ báo cáo.
"""

from __future__ import annotations

import os
import json
import math
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from .v1_stage_metrics import stage
except ImportError:
    from v1_stage_metrics import stage

logger = logging.getLogger(__name__)


class QCPolicy(str, Enum):
    REPORT_ONLY = "REPORT_ONLY"
    WARN = "WARN"
    BLOCK = "BLOCK"


class CriticalQualityError(RuntimeError):
    """A proven integrity failure cannot be published, even in REPORT_ONLY."""


def critical_failures(checks):
    names = {"final_video_exists", "final_video_size", "duration_alignment",
             "source_geometry_fps", "video_stream_integrity", "ass_subtitle_file",
             "final_stream_layout", "ocr_coordinate_coverage",
             "tts_audio_files", "tts_cue_coverage", "tts_no_collisions",
             "tts_timeline_bounds", "no_cjk_residual", "no_empty_subtitles",
             "visual_pixel_coverage"}
    return [c for c in checks if c.status == "FAIL" and c.name in names]


class QCCategory(str, Enum):
    FILE_INTEGRITY = "file_integrity"          # 1. Tính toàn vẹn của tệp video và luồng âm thanh/hình ảnh
    AV_SYNC = "av_sync"                        # 2. Độ lệch đồng bộ âm thanh - hình ảnh (A/V sync)
    ASR_COVERAGE = "asr_coverage"              # 3. Độ bao phủ câu thoại ASR
    TRANSLATION_INTEGRITY = "translation"       # 4. Tính toàn vẹn của bản dịch (không sót CJK, đủ câu)
    SUBTITLE_STYLING = "subtitle_styling"      # 5. Phụ đề ASS hiển thị chuẩn xác
    TTS_TIMING_FIT = "tts_timing_fit"          # 6. Khớp thời lượng TTS (không đè câu sau)
    AUDIO_BALANCE = "audio_balance"            # 7. Cân bằng âm lượng (không clipping)
    RENDER_CONTINUITY = "render_continuity"    # 8. Tính liên tục khung hình render NVENC


@dataclass
class QCCheckItem:
    category: str
    name: str
    status: str  # "PASS", "WARN", "FAIL"
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


def _seg_val(s, key, default=None):
    if s is None:
        return default
    if isinstance(s, dict):
        if "__object_fields__" in s:
            s = s["__object_fields__"]
        return s.get(key, default)
    return getattr(s, key, default)


@dataclass
class QCReport:
    job_id: str
    video_name: str
    overall_status: str  # "PASSED", "PASSED_WITH_WARNINGS", "FAILED"
    policy: str
    passed_count: int = 0
    warning_count: int = 0
    failure_count: int = 0
    checks: List[QCCheckItem] = field(default_factory=list)
    created_at: float = field(default_factory=__import__("time").time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


def _check_cjk(text: str) -> bool:
    """Kiểm tra xem câu có sót ký tự chữ Hán (CJK & CJK Extension A) hay không."""
    for ch in text:
        if ("\u4e00" <= ch <= "\u9fff") or ("\u3400" <= ch <= "\u4dbf"):
            return True
    return False



def _qc_ts_seconds(value: Any) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _map_normalized_box_to_frame(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    out_w: int,
    out_h: int,
    source_size: Optional[tuple[int, int]] = None,
    padding: bool = True,
) -> tuple[int, int, int, int]:
    """Ánh xạ bbox chuẩn hóa (0..1) từ video gốc sang tọa độ pixel trên video thành phẩm."""
    if source_size and source_size[0] > 0 and source_size[1] > 0:
        src_w, src_h = source_size
        src_ar = src_w / src_h
        out_ar = out_w / max(1, out_h)
        if abs(src_ar - out_ar) >= 0.03:
            scale = min(out_w / src_w, out_h / src_h)
            pad_x = (out_w - src_w * scale) * 0.5
            pad_y = (out_h - src_h * scale) * 0.5
            px0 = pad_x + x0 * src_w * scale
            py0 = pad_y + y0 * src_h * scale
            px1 = pad_x + x1 * src_w * scale
            py1 = pad_y + y1 * src_h * scale
        else:
            px0, py0, px1, py1 = x0 * out_w, y0 * out_h, x1 * out_w, y1 * out_h
    else:
        px0, py0, px1, py1 = x0 * out_w, y0 * out_h, x1 * out_w, y1 * out_h

    pad_px = max(24, int(round(out_w * 0.05))) if padding else 0
    pad_py = max(20, int(round(out_h * 0.04))) if padding else 0
    rx0 = max(0, min(out_w - 2, int(round(px0)) - pad_px))
    ry0 = max(0, min(out_h - 2, int(round(py0)) - pad_py))
    rx1 = max(rx0 + 2, min(out_w, int(round(px1)) + pad_px))
    ry1 = max(ry0 + 2, min(out_h, int(round(py1)) + pad_py))
    return rx0, ry0, rx1, ry1


def _transition_pixel_candidates(segments, fps=30.0, limit=192):
    """Check source-caption boundaries independently of fitted TTS timing."""
    tracks = []
    for i, seg in enumerate(segments or []):
        for block in _seg_val(seg, "tracking_blocks", []) or []:
            try:
                box = tuple(float(_seg_val(block, k)) for k in
                            ("x_pct", "y_pct", "max_x_pct", "max_y_pct"))
                start, end = (_qc_ts_seconds(_seg_val(block, k)) for k in ("start", "end"))
                if not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1 and 0 <= start < end):
                    continue
            except (TypeError, ValueError):
                continue
            if _seg_val(block, "is_static") or _seg_val(block, "is_packaging"):
                continue
            tracks.append(dict(index=_seg_val(seg, "index", i + 1), start=start, end=end,
                               text=str(_seg_val(block, "text", "")), box=box))
    tracks.sort(key=lambda b: (b["start"], b["end"]))
    events = []
    for i, current in enumerate(tracks):
        previous = tracks[i - 1] if i else None
        if previous and (current["text"] == previous["text"]
                         and current["start"] <= previous["end"] + .001
                         and max(abs(a-b) for a, b in zip(current["box"], previous["box"])) < .05):
            continue
        box = current["box"]
        if previous:
            old = previous["box"]
            box = min(box[0], old[0]), min(box[1], old[1]), max(box[2], old[2]), max(box[3], old[3])
            events.append((previous["end"], previous["index"], box))
        events.append((current["start"], current["index"], box))
    if tracks:
        events.append((tracks[-1]["end"], tracks[-1]["index"], tracks[-1]["box"]))
    candidates = {}
    for boundary, index, box in events:
        # The wider offset also audits legacy sampling uncertainty, not only
        # the timestamp a possibly stale mask claims is the transition.
        for offset in (-.12, -1/fps, 0., 1/fps):
            time_s = round(max(0., boundary + offset), 6)
            candidates[(time_s, box)] = dict(index=index, mid_s=time_s, box=box, transition=True)
    ordered = sorted(candidates.values(), key=lambda c: c["mid_s"])
    total = len(ordered)
    if total > limit:
        selected = sorted({round(i * (total - 1) / (limit - 1)) for i in range(limit)})
        ordered = [ordered[i] for i in selected]
    return ordered, total


def _qc_stop(stop_check):
    if stop_check is not None and stop_check():
        raise RuntimeError("QC stop requested")


def _qc_progress(callback, phase, completed, total):
    if callback is not None:
        callback({"phase": phase, "completed": completed, "total": total})


def _sequential_qc_frames(cap, requests, cv2, metrics, duration=None, stop_check=None):
    """Select displayed frames by real PTS, using at most two decoded images.

    A frame remains displayed until the following frame's timestamp. One-frame
    lookahead avoids rounding VFR timestamps to an average-FPS frame number.
    Requests for different ROIs can share the same decoded frame.
    """
    previous_pts = None
    raw_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    expected_count = int(round(raw_count)) if math.isfinite(raw_count) and raw_count > 0 else None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.)
    fps = fps if math.isfinite(fps) and fps > 0 else 30.

    def read_frame():
        nonlocal previous_pts
        _qc_stop(stop_check)
        started = time.monotonic()
        try:
            ok, image = cap.read()
            if not ok:
                if expected_count is not None and metrics["decoded_frames"] < expected_count:
                    raise ValueError("QC decoder stopped before the expected end of video")
                return None
            pts = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
        finally:
            metrics["decode_seconds"] += time.monotonic() - started
        metrics["decoded_frames"] += 1
        # FFmpeg/OpenCV can report a small negative preroll for the first
        # reordered frame of an otherwise zero-origin NVENC stream. That frame
        # is already displayed at time zero; only this first timestamp may be
        # clamped. Later timestamps must still strictly increase.
        if previous_pts is None and -max(.1, 1.5 / fps) <= pts < 0:
            pts = 0.
            metrics["initial_preroll_clamped"] = True
        if (not math.isfinite(pts) or pts < 0
                or (previous_pts is not None and pts <= previous_pts)):
            raise ValueError("QC decoder timestamps must increase")
        if image is None or not hasattr(image, "shape") or getattr(image, "ndim", 0) < 2:
            raise ValueError("QC decoder returned an invalid frame")
        previous_pts = pts
        return pts, metrics["decoded_frames"] - 1, image

    current = read_frame()
    following = read_frame() if current is not None else None
    for item in sorted(requests, key=lambda row: row["mid_s"]):
        _qc_stop(stop_check)
        target = float(item["mid_s"])
        if not math.isfinite(target) or target < 0:
            continue
        while following is not None and following[0] <= target + 1e-7:
            current, following = following, read_frame()
        if current is None or target + 1e-7 < current[0]:
            continue
        if following is None and target > current[0] + 1e-7:
            # The last frame's display interval needs independently probed
            # duration; frame_count / average_fps is unsafe for VFR video.
            if duration is None or expected_count is None or target >= duration:
                continue
        yield item, current[2], current[0], current[1]


def check_rendered_pixel_coverage(
    final_video_path: str | Path,
    translated_subtitles: Optional[Sequence[Any]] = None,
    ass_subtitle_path: Optional[str | Path] = None,
    source_size: Optional[tuple[int, int]] = None,
    max_samples: int = 4,
    enabled: bool = True,
    rendered_duration_s: Optional[float] = None,
    progress_callback=None,
    stop_check=None,
) -> QCCheckItem:
    """
    Hậu kiểm giữa câu và các biên chuyển phụ đề nguồn (tối đa 192 mẫu biên):
    cắt vùng OCR phụ đề gốc từ video xuất xưởng và quét lại bằng PP-OCRv6.
    Chữ Hán còn nằm trong vùng phụ đề nguồn là FAIL; chữ ngoài vùng chỉ WARN.
    """
    if not enabled:
        return QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value,
            "visual_pixel_coverage",
            "UNKNOWN",
            "Kiểm tra pixel sau render đang tắt (V1_QC_VISUAL_PIXEL_CHECK=False)",
        )
    v_path = Path(final_video_path)
    if not v_path.is_file():
        return QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value,
            "visual_pixel_coverage",
            "UNKNOWN",
            "Không tìm thấy tệp video xuất xưởng để quét pixel",
        )

    candidates = []
    for i, seg in enumerate(translated_subtitles or []):
        if _seg_val(seg, "ocr_mask_status") != "located":
            continue
        bb = _seg_val(seg, "best_block")
        if bb is None:
            continue
        if _seg_val(bb, "is_static") is True or _seg_val(bb, "is_packaging") is True:
            continue
        try:
            x0 = float(_seg_val(bb, "x_pct", 0.0))
            x1 = float(_seg_val(bb, "max_x_pct", 0.0))
            y0 = float(_seg_val(bb, "y_pct", 0.0))
            y1 = float(_seg_val(bb, "max_y_pct", 0.0))
        except (TypeError, ValueError):
            continue
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            continue
        start_s = _qc_ts_seconds(_seg_val(seg, "start", 0.0))
        end_s = _qc_ts_seconds(_seg_val(seg, "end", 0.0))
        if (end_s - start_s) < 0.40:
            continue
        candidates.append({
            "index": _seg_val(seg, "index", i + 1),
            "mid_s": round((start_s + end_s) * 0.5, 3),
            "box": (x0, y0, x1, y1),
        })

    if not candidates and not any(_seg_val(seg, "tracking_blocks", []) for seg in translated_subtitles or []):
        return QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value,
            "visual_pixel_coverage",
            "UNKNOWN",
            "Chưa đủ mẫu câu có tọa độ OCR hợp lệ (>= 0.4s) để hậu kiểm pixel",
        )

    max_samples = max(1, int(max_samples))
    if len(candidates) <= max_samples:
        sampled = candidates
    elif max_samples == 1:
        sampled = [candidates[len(candidates) // 2]]
    else:
        pick = sorted({round(i * (len(candidates) - 1) / (max_samples - 1)) for i in range(max_samples)})
        sampled = [candidates[idx] for idx in pick]

    try:
        import cv2
    except Exception as exc:
        return QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value,
            "visual_pixel_coverage",
            "UNKNOWN",
            f"Bỏ qua hậu kiểm pixel do thiếu OpenCV ({exc})",
        )

    source_text = []
    for seg in translated_subtitles or []:
        for block in _seg_val(seg, "tracking_blocks", []) or []:
            if _seg_val(block, "is_static") or _seg_val(block, "is_packaging"):
                continue
            text = "".join(c for c in (_seg_val(block, "text") or "") if "\u3400" <= c <= "\u9fff")
            if len(text) >= 2:
                source_text.append((_qc_ts_seconds(_seg_val(block, "start", 0)),
                                    _qc_ts_seconds(_seg_val(block, "end", 0)), text))

    stop_error = None
    original_stop_check = stop_check

    def checked_stop():
        nonlocal stop_error
        try:
            _qc_stop(original_stop_check)
        except Exception as exc:
            stop_error = exc
            raise

    stop_check = checked_stop
    _qc_stop(stop_check)
    crops = []
    crop_meta = []
    batch_rows = []
    metrics = {"decoded_frames": 0, "unique_decoded_frames": 0, "seek_count": 0,
               "decode_seconds": 0.0, "ocr_seconds": 0.0}
    used_frames = set()
    ocr_started = False
    duration = rendered_duration_s
    if duration is None:
        try:
            from v1_media_streams import probe_main_video
            duration = probe_main_video(v_path).duration
        except Exception:
            duration = None
    if duration is not None and (not math.isfinite(float(duration)) or float(duration) <= 0):
        duration = None

    def flush_crops():
        nonlocal ocr_started
        if not crops:
            return
        _qc_stop(stop_check)
        from ocr_utils import _readtext_batch
        from v1_gpu_gatekeeper import gpu_gatekeeper
        started = time.monotonic()
        try:
            with gpu_gatekeeper("visual_ocr", timeout_seconds=120.0, min_free_mb=1000.0):
                ocr_started = True
                rows = _readtext_batch(crops)
            if len(rows or []) != len(crops):
                raise ValueError("OCR hậu kiểm trả thiếu kết quả")
            batch_rows.extend(rows)
            crops.clear()
        finally:
            metrics["ocr_seconds"] += time.monotonic() - started
        _qc_stop(stop_check)
        _qc_progress(progress_callback, "ocr", len(batch_rows), requested_frames)

    requested_frames = 0
    cap = cv2.VideoCapture(str(v_path))
    try:
        if not cap.isOpened():
            return QCCheckItem(
                QCCategory.SUBTITLE_STYLING.value,
                "visual_pixel_coverage",
                "UNKNOWN",
                "Không mở được video xuất xưởng bằng OpenCV để hậu kiểm pixel",
            )
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.)
        if not math.isfinite(fps) or fps <= 0:
            fps = 30.
        transitions, transition_total = _transition_pixel_candidates(translated_subtitles, fps)
        sampled = transitions + sampled
        sampled = [item for item in sampled if duration is None or item["mid_s"] < duration]
        requested_frames = len(sampled)
        _qc_progress(progress_callback, "decode", 0, requested_frames)
        for item, frame, decoded_pts, ordinal in _sequential_qc_frames(
                cap, sampled, cv2, metrics, duration=duration, stop_check=stop_check):
            fh, fw = frame.shape[:2]
            if fw < 32 or fh < 32:
                continue
            x0, y0, x1, y1 = item["box"]
            rx0, ry0, rx1, ry1 = _map_normalized_box_to_frame(x0, y0, x1, y1, fw, fh, source_size)
            crop = frame[ry0:ry1, rx0:rx1]
            if crop is None or crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 16:
                continue
            crops.append(crop.copy())
            used_frames.add(ordinal)
            crop_meta.append({
                "index": item["index"],
                "time_s": item["mid_s"],
                "roi": [rx0, ry0, rx1, ry1],
                "target_roi": _map_normalized_box_to_frame(
                    x0, y0, x1, y1, fw, fh, source_size, padding=False),
                "source_text": {text for start, end, text in source_text
                                if start - .12 <= item["mid_s"] <= end + .12},
                "transition": item.get("transition", False),
                "decoded_time_s": decoded_pts,
            })
            _qc_progress(progress_callback, "decode", len(crop_meta), requested_frames)
            if len(crops) >= 8:
                flush_crops()
        flush_crops()
    except Exception as exc:
        if stop_error is not None:
            raise stop_error
        return QCCheckItem(QCCategory.SUBTITLE_STYLING.value, "visual_pixel_coverage", "UNKNOWN",
                           f"Chưa xác minh đủ khung hình QC ({exc})", details=metrics)
    finally:
        cap.release()
        if ocr_started:
            from ocr_utils import release_ocr_reader
            release_ocr_reader()
    metrics["unique_decoded_frames"] = len(used_frames)
    metrics["decode_seconds"] = round(metrics["decode_seconds"], 4)
    metrics["ocr_seconds"] = round(metrics["ocr_seconds"], 4)
    logger.info("QC sequential frames=%d samples=%d unique_frames=%d seeks=0 decode_seconds=%.3f ocr_seconds=%.3f",
                metrics["decoded_frames"], len(crop_meta), len(used_frames),
                metrics["decode_seconds"], metrics["ocr_seconds"])

    if not crop_meta:
        return QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value,
            "visual_pixel_coverage",
            "UNKNOWN",
            "Không trích được khung hình hợp lệ từ video xuất xưởng để hậu kiểm pixel",
        )

    from ocr_utils import _visual_chinese_count
    if len(batch_rows or []) != len(crop_meta):
        return QCCheckItem(QCCategory.SUBTITLE_STYLING.value, "visual_pixel_coverage", "UNKNOWN",
                           "OCR hậu kiểm trả thiếu kết quả; chưa xác minh đủ các khung hình")
    leaks = []
    for meta, rows in zip(crop_meta, batch_rows or []):
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            text = str(row[1] or "").strip()
            prob = float(row[2]) if len(row) >= 3 and row[2] is not None else 1.0
            cjk_cnt = _visual_chinese_count(text)
            if cjk_cnt >= 2 and prob >= 0.60:
                # Padded OCR context can contain valid packaging/scene text.
                # Only block when the recognized glyphs overlap the source
                # caption itself, rather than treating every nearby CJK as a leak.
                in_caption = False
                try:
                    points = row[0]
                    x_min = min(float(p[0]) for p in points) + meta["roi"][0]
                    x_max = max(float(p[0]) for p in points) + meta["roi"][0]
                    y_min = min(float(p[1]) for p in points) + meta["roi"][1]
                    y_max = max(float(p[1]) for p in points) + meta["roi"][1]
                    tx0, ty0, tx1, ty1 = meta["target_roi"]
                    area = max(0., x_max - x_min) * max(0., y_max - y_min)
                    overlap = max(0., min(x_max, tx1) - max(x_min, tx0)) * max(0., min(y_max, ty1) - max(y_min, ty0))
                    han = "".join(c for c in text if "\u3400" <= c <= "\u9fff")
                    matches_source = not source_text or any(
                        han[i:i + 2] in expected for expected in meta["source_text"]
                        for i in range(len(han) - 1))
                    # High-confidence glyphs in the verified caption box remain
                    # a leak even when OCR spelling differs from source text.
                    in_caption = (area > 0 and overlap / area >= 0.5
                                  and (prob >= 0.90 or matches_source))
                except (TypeError, ValueError, IndexError):
                    pass
                leaks.append({
                    "index": meta["index"],
                    "time_s": meta["time_s"],
                    "roi": meta["roi"],
                    "detected_text": text[:40],
                    "cjk_chars": cjk_cnt,
                    "confidence": round(prob, 3),
                    "inside_caption": in_caption,
                })

    if leaks:
        confirmed = [leak for leak in leaks if leak["inside_caption"]]
        visible_leaks = confirmed or leaks
        times_str = ", ".join(f"{lk['time_s']:.1f}s" for lk in visible_leaks[:3])
        return QCCheckItem(
            QCCategory.SUBTITLE_STYLING.value,
            "visual_pixel_coverage",
            "FAIL" if confirmed else "WARN",
            (f"Còn chữ Hán trong vùng phụ đề nguồn ở {times_str}; cần sửa trước khi xuất video"
             if confirmed else f"Phát hiện chữ Hán gần vùng phụ đề ở {times_str}; cần kiểm tra ngữ cảnh"),
            details={
                "sampled_frames": len(crop_meta),
                **metrics,
                "leaks": visible_leaks[:5],
                "ass_subtitle_path": str(ass_subtitle_path) if ass_subtitle_path else None,
                "transition_candidates": transition_total,
                "leak_count": len(leaks),
                "confirmed_leak_count": len(confirmed),
                "leak_times_s": sorted({leak["time_s"] for leak in leaks}),
            },
        )

    return QCCheckItem(
        QCCategory.SUBTITLE_STYLING.value,
        "visual_pixel_coverage",
        "WARN" if transition_total > len(transitions) or len(crop_meta) < requested_frames else "PASS",
        (f"Đã quét {len(crop_meta)} khung hình; hậu kiểm chưa đủ do giới hạn lấy mẫu hoặc lỗi giải mã"
         if transition_total > len(transitions) or len(crop_meta) < requested_frames else
         f"Đã quét pixel thực tế {len(crop_meta)} khung hình xuất xưởng: không phát hiện chữ gốc lòi ra"),
        details={
            "sampled_frames": len(crop_meta),
            **metrics,
            "sample_times_s": [m["time_s"] for m in crop_meta],
            "decoded_sample_times_s": [m["decoded_time_s"] for m in crop_meta],
            "transition_candidates": transition_total,
            "transition_samples": sum(m["transition"] for m in crop_meta),
            "transition_sampling_capped": transition_total > len(transitions),
            "requested_frames": requested_frames,
        },
    )


@stage("quality_gate")
def run_quality_gate(
    job_id: str,
    final_video_path: str | Path,
    original_video_path: Optional[str | Path] = None,
    translated_subtitles: Optional[Sequence[Any]] = None,
    dubbing_audio_files: Optional[Sequence[Dict[str, Any]]] = None,
    mixed_audio_path: Optional[str | Path] = None,
    ass_subtitle_path: Optional[str | Path] = None,
    policy: Optional[str] = None,
    workspace_path: Optional[str | Path] = None,
    enable_visual_pixel_qc: Optional[bool] = None,
    progress_callback=None,
    stop_check=None,
) -> QCReport:
    """
    Chạy 8 nhóm kiểm tra tự động cho video thành phẩm và xuất báo cáo JSON.
    """
    effective_policy = (policy or os.getenv("V1_QC_POLICY", QCPolicy.BLOCK.value)).upper()
    _qc_stop(stop_check)
    if effective_policy not in (QCPolicy.REPORT_ONLY.value, QCPolicy.WARN.value, QCPolicy.BLOCK.value):
        effective_policy = QCPolicy.BLOCK.value

    v_path = Path(final_video_path)
    video_name = v_path.name if v_path.name else "unknown_video"
    checks: List[QCCheckItem] = []

    # 1. FILE INTEGRITY
    if not v_path.is_file():
        checks.append(QCCheckItem(
            category=QCCategory.FILE_INTEGRITY.value,
            name="final_video_exists",
            status="FAIL",
            message=f"Tệp video thành phẩm không tồn tại: {v_path}",
        ))
    else:
        v_size = v_path.stat().st_size
        if v_size < 10240:
            checks.append(QCCheckItem(
                category=QCCategory.FILE_INTEGRITY.value,
                name="final_video_size",
                status="FAIL",
                message=f"Tệp video quá nhỏ ({v_size} bytes), có thể bị lỗi render",
                details={"size_bytes": v_size},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.FILE_INTEGRITY.value,
                name="final_video_size",
                status="PASS",
                message=f"Tệp video hợp lệ ({v_size / 1024 / 1024:.2f} MB)",
                details={"size_bytes": v_size},
            ))

    # Compare source/output metadata: reading an audio file alone proves no sync.
    try:
        from v1_media_streams import probe_main_video
        import soundfile as sf
        output = probe_main_video(v_path)
        valid_layout = output.video_stream_count == 1 and output.audio_stream_count == 1 and not output.has_cover
        checks.append(QCCheckItem(QCCategory.FILE_INTEGRITY.value, "final_stream_layout",
            "PASS" if valid_layout else "FAIL", "Video thành phẩm cần đúng một luồng hình và một luồng âm"))
        source = probe_main_video(original_video_path) if original_video_path else None
        audio_duration = sf.info(str(mixed_audio_path)).duration if mixed_audio_path else 0
        tolerance = max(0.40, 3.0 / max(1.0, output.fps))
        duration_ok = output.duration > 0 and audio_duration > 0 and abs(output.duration - audio_duration) <= tolerance
        if source:
            duration_ok = duration_ok and source.duration > 0 and abs(output.duration - source.duration) <= tolerance

            # Nới rộng hoàn toàn độ phân giải và FPS theo yêu cầu:
            # Cho phép mọi độ phân giải (2K, 4K, 1080p, 720p, 9:16 dọc, 16:9 ngang, 4:3, 1:1, v.v.)
            # và mọi mức FPS (24, 25, 30, 60, 120, VFR chuẩn hóa).
            valid_output_geom = output.width > 0 and output.height > 0 and output.fps > 0

            if not valid_output_geom:
                geom_fps_status = "FAIL"
                geom_fps_msg = f"Video thành phẩm có độ phân giải hoặc FPS không hợp lệ ({output.width}x{output.height}@{output.fps:.2f})"
            else:
                geom_fps_status = "PASS"
                geometry_exact = (output.width, output.height) == (source.width, source.height)
                fps_diff = abs(output.fps - source.fps)
                fps_pass_tol = max(0.5, source.fps * 0.05)

                if geometry_exact and fps_diff <= fps_pass_tol:
                    geom_fps_msg = f"Độ phân giải và FPS khớp chuẩn video gốc ({output.width}x{output.height} @ {output.fps:.2f} FPS)"
                else:
                    # Ghi nhận chi tiết chuẩn hóa biên tập (chuyển khung hình hoặc FPS)
                    geom_fps_msg = f"Độ phân giải và FPS chuẩn hóa thành công ({source.width}x{source.height}@{source.fps:.2f} -> {output.width}x{output.height}@{output.fps:.2f} FPS)"

            checks.append(QCCheckItem(QCCategory.RENDER_CONTINUITY.value, "source_geometry_fps",
                geom_fps_status, geom_fps_msg,
                {"source": asdict(source), "output": asdict(output)}))
        checks.append(QCCheckItem(QCCategory.AV_SYNC.value, "duration_alignment",
            "PASS" if duration_ok else "FAIL", "Đối chiếu thời lượng hình/âm thanh (không xác nhận lip-sync)",
            {"video_duration": output.duration, "audio_duration": audio_duration}))
    except Exception as exc:
        checks.append(QCCheckItem(QCCategory.AV_SYNC.value, "duration_alignment",
            "WARN", f"Chưa xác minh được đồng bộ: {exc}"))

    # 3. ASR COVERAGE
    all_ocr_located = bool(translated_subtitles) and all(
        _seg_val(s, "ocr_mask_status") == "located" and _seg_val(s, "best_block") is not None
        for s in translated_subtitles
    )
    if translated_subtitles is not None:
        seg_count = len(translated_subtitles)
        if seg_count == 0:
            checks.append(QCCheckItem(
                category=QCCategory.ASR_COVERAGE.value,
                name="transcription_segments_count",
                status="WARN",
                message="Không có câu thoại nào được nhận diện (video im lặng hoặc lỗi ASR)",
            ))
        elif all_ocr_located:
            checks.append(QCCheckItem(
                category=QCCategory.ASR_COVERAGE.value,
                name="transcription_segments_count",
                status="PASS",
                message=f"Đã nhận diện và đối chiếu tọa độ OCR cho {seg_count} câu thoại",
                details={"segment_count": seg_count, "ocr_verified": True},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.ASR_COVERAGE.value,
                name="transcription_segments_count",
                status="WARN",
                message=f"Có {seg_count} câu; chưa có bản chép lời chuẩn để xác minh không sót thoại",
                details={"segment_count": seg_count},
            ))

    # 4. TRANSLATION INTEGRITY
    if translated_subtitles:
        cjk_leaks = []
        empty_lines = []
        for i, s in enumerate(translated_subtitles):
            txt = str(_seg_val(s, "content") or _seg_val(s, "text") or "").strip()
            s_idx = _seg_val(s, "index", i + 1)
            if not txt:
                empty_lines.append(s_idx)
            elif _check_cjk(txt):
                cjk_leaks.append({"index": s_idx, "text": txt[:30]})

        if cjk_leaks:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_cjk_residual",
                status="FAIL",
                message=f"Phát hiện {len(cjk_leaks)} câu còn sót chữ Hán",
                details={"cjk_leaks": cjk_leaks[:5]},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_cjk_residual",
                status="PASS",
                message="100% câu dịch sạch tiếng Trung",
            ))

        if empty_lines:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_empty_subtitles",
                status="FAIL",
                message=f"Có {len(empty_lines)} câu bị rỗng nội dung",
                details={"empty_indices": empty_lines[:10]},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.TRANSLATION_INTEGRITY.value,
                name="no_empty_subtitles",
                status="PASS",
                message="Không có câu dịch nào bị rỗng",
            ))

    # 5. SUBTITLE STYLING
    if ass_subtitle_path:
        ass_p = Path(ass_subtitle_path)
        if ass_p.is_file() and ass_p.stat().st_size > 100:
            ass_text = ass_p.read_text(encoding="utf-8-sig")
            valid_ass = "[Script Info]" in ass_text and "[Events]" in ass_text
            events = [line for line in ass_text.splitlines() if line.startswith("Dialogue:")]
            valid_ass = valid_ass and bool(events)
            def ass_seconds(value):
                hours, minutes, seconds = value.strip().split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            try:
                for event in events:
                    fields = event.split(",", 9)
                    start, end = ass_seconds(fields[1]), ass_seconds(fields[2])
                    if start < 0 or end <= start or not fields[9].strip():
                        valid_ass = False
            except (ValueError, IndexError):
                valid_ass = False
            checks.append(QCCheckItem(
                category=QCCategory.SUBTITLE_STYLING.value,
                name="ass_subtitle_file",
                status="PASS" if valid_ass else "FAIL",
                message="Kiểm tra cấu trúc và timeline ASS (chưa xác minh hình ảnh che chữ)",
            ))
            has_ass_cover_boxes = valid_ass and all_ocr_located and ("BgStyle" in ass_text or "\\p1" in ass_text)
            if has_ass_cover_boxes:
                checks.append(QCCheckItem(
                    QCCategory.SUBTITLE_STYLING.value,
                    "visual_mask_coverage",
                    "PASS",
                    "Đã xác minh tọa độ OCR và lớp hộp che nền (BgStyle) trong tệp ASS",
                ))
            else:
                checks.append(QCCheckItem(
                    QCCategory.SUBTITLE_STYLING.value,
                    "visual_mask_coverage",
                    "UNKNOWN",
                    "Cần kiểm tra video thực tế để xác nhận nền trắng che đủ chữ gốc",
                ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.SUBTITLE_STYLING.value,
                name="ass_subtitle_file",
                status="WARN",
                message="Tệp phụ đề ASS thiếu hoặc có dung lượng 0 bytes",
            ))
    else:
        checks.append(QCCheckItem(
            category=QCCategory.SUBTITLE_STYLING.value,
            name="ass_subtitle_file",
            status="UNKNOWN",
            message="Không có tệp phụ đề ASS được chỉ định",
        ))

    # Coordinate integrity is measurable; pixel-level coverage stays UNKNOWN.
    if translated_subtitles:
        try:
            from .v1_ocr_geometry import geometry_summary, validate_mask_geometry
        except ImportError:
            from v1_ocr_geometry import geometry_summary, validate_mask_geometry
        geometry = geometry_summary(translated_subtitles)
        geometry_status = 'PASS'
        try:
            validate_mask_geometry(translated_subtitles)
            if any(key != 'located' for key in geometry['counts']):
                geometry_status = 'WARN'
        except RuntimeError:
            geometry_status = 'FAIL'
        checks.append(QCCheckItem(QCCategory.SUBTITLE_STYLING.value,
            'ocr_coordinate_coverage', geometry_status,
            'OCR coordinate audit (not a guarantee of pixel-level coverage)', geometry))

    # 5b. Confirmed caption leaks participate in the normal BLOCK policy.
    if enable_visual_pixel_qc is None:
        try:
            from v1_feature_flags import get_feature_flags
            enable_visual_pixel_qc = bool(get_feature_flags(workspace_path).get("V1_QC_VISUAL_PIXEL_CHECK", True))
        except Exception:
            enable_visual_pixel_qc = True

    if enable_visual_pixel_qc:
        src_geom_size = (
            (source.width, source.height)
            if ("source" in locals() and source is not None and source.width > 0 and source.height > 0)
            else None
        )
        pixel_check = check_rendered_pixel_coverage(
            final_video_path=v_path,
            translated_subtitles=translated_subtitles,
            ass_subtitle_path=ass_subtitle_path,
            source_size=src_geom_size,
            enabled=True,
            rendered_duration_s=output.duration if "output" in locals() else None,
            progress_callback=progress_callback,
            stop_check=stop_check,
        )
        checks.append(pixel_check)

    # 6. TTS TIMING FIT: compare the actual files, not cached duration labels.
    measured_dubs = []
    missing_audio = []
    for dub in dubbing_audio_files or []:
        try:
            import soundfile as sf
            duration = float(sf.info(str(dub.get("path", ""))).duration)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Empty audio")
            start = float(dub.get("start", 0.0))
            if not math.isfinite(start) or start < 0:
                raise ValueError("Invalid start")
            measured_dubs.append({**dub, "duration": duration})
        except Exception as exc:
            missing_audio.append({"index": dub.get("index"), "error": str(exc)})
    if missing_audio:
        checks.append(QCCheckItem(QCCategory.TTS_TIMING_FIT.value, "tts_audio_files", "FAIL",
            f"{len(missing_audio)} file TTS thiếu hoặc không đọc được", {"missing": missing_audio[:10]}))
    if translated_subtitles:
        expected = {_seg_val(s, "index", i + 1) for i, s in enumerate(translated_subtitles) if str(_seg_val(s, "content") or _seg_val(s, "text") or "").strip()}
        actual = {d.get("index") for d in measured_dubs}
        complete = expected == actual and len(measured_dubs) == len(expected)
        checks.append(QCCheckItem(QCCategory.TTS_TIMING_FIT.value, "tts_cue_coverage",
            "PASS" if complete else "FAIL", "Đối chiếu từng câu phụ đề với audio TTS",
            {"expected_count": len(expected), "audio_count": len(measured_dubs)}))
    dubbing_audio_files = measured_dubs
    if measured_dubs:
        try:
            from v1_speech_guard import validate_speech_timeline
            final_duration = output.duration if 'output' in locals() else None
            validate_speech_timeline(measured_dubs, total_duration=final_duration)
        except Exception as exc:
            checks.append(QCCheckItem(QCCategory.TTS_TIMING_FIT.value, "tts_timeline_bounds", "FAIL", str(exc)))
    if dubbing_audio_files:
        sorted_dubs = sorted(dubbing_audio_files, key=lambda d: float(d.get("start", 0.0)))
        collision_count = 0
        for i in range(len(sorted_dubs) - 1):
            cur_s = float(sorted_dubs[i].get("start", 0.0))
            cur_dur = float(sorted_dubs[i].get("duration", 0.0) or sorted_dubs[i].get("actual_audio_duration", 0.0))
            next_s = float(sorted_dubs[i + 1].get("start", 0.0))
            from v1_speech_guard import BOUNDARY_TOLERANCE_S
            if cur_s + cur_dur > next_s + BOUNDARY_TOLERANCE_S:  # Aligned with speech guard tolerance
                collision_count += 1

        if collision_count > 0:
            checks.append(QCCheckItem(
                category=QCCategory.TTS_TIMING_FIT.value,
                name="tts_no_collisions",
                status="FAIL",
                message=f"Phát hiện {collision_count} vị trí có nguy cơ chớm đè câu",
                details={"collision_count": collision_count},
            ))
        else:
            checks.append(QCCheckItem(
                category=QCCategory.TTS_TIMING_FIT.value,
                name="tts_no_collisions",
                status="PASS",
                message="Không phát hiện đè câu theo thời lượng audio được cung cấp",
            ))
    else:
        checks.append(QCCheckItem(
            category=QCCategory.TTS_TIMING_FIT.value,
            name="tts_no_collisions",
            status="UNKNOWN",
            message="Chưa có dữ liệu danh sách audio lồng tiếng để kiểm tra va chạm",
        ))

    # 7. AUDIO BALANCE
    if mixed_audio_path and Path(mixed_audio_path).is_file():
        try:
            import soundfile as sf
            import numpy as np
            peak = max((float(np.max(np.abs(block))) for block in sf.blocks(str(mixed_audio_path), blocksize=65536, dtype="float32")), default=0.0)
            peak_dbfs = 20 * math.log10(max(peak, 1e-12))
            if peak_dbfs > -0.05:
                checks.append(QCCheckItem(
                    category=QCCategory.AUDIO_BALANCE.value,
                    name="peak_ceiling",
                    status="WARN",
                    message=f"Đỉnh âm thanh chạm ngưỡng clipping ({peak_dbfs:.2f} dBFS)",
                    details={"peak_dbfs": peak_dbfs},
                ))
            else:
                checks.append(QCCheckItem(
                    category=QCCategory.AUDIO_BALANCE.value,
                    name="peak_ceiling",
                    status="PASS",
                    message=f"Đỉnh âm thanh an toàn ({peak_dbfs:.2f} dBFS <= -0.5 dBFS)",
                    details={"peak_dbfs": peak_dbfs},
                ))
        except Exception:
            pass
    elif mixed_audio_path:
        checks.append(QCCheckItem(
            category=QCCategory.AUDIO_BALANCE.value,
            name="peak_ceiling",
            status="WARN",
            message=f"Không tìm thấy file audio mixed để đo peak: {mixed_audio_path}",
        ))

    # 8. RENDER CONTINUITY & FRAME DECODING (PROBE THẬT)
    if not v_path.is_file():
        checks.append(QCCheckItem(
            category=QCCategory.RENDER_CONTINUITY.value,
            name="video_stream_integrity",
            status="FAIL",
            message="Không tìm thấy tệp video để kiểm tra stream rendering",
        ))
    else:
        try:
            import cv2
            cap = cv2.VideoCapture(str(v_path))
            is_open = cap.isOpened()
            frame_cnt = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if is_open else 0
            v_fps = float(cap.get(cv2.CAP_PROP_FPS)) if is_open else 0.0
            v_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if is_open else 0
            v_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if is_open else 0
            ret, _ = cap.read() if is_open else (False, None)
            cap.release()

            if is_open and frame_cnt > 0 and v_fps > 0 and v_w > 0 and v_h > 0 and ret:
                checks.append(QCCheckItem(
                    category=QCCategory.RENDER_CONTINUITY.value,
                    name="video_stream_integrity",
                    status="PASS",
                    message=f"Khung hình đầu giải mã được; chưa kiểm tra toàn bộ video ({v_w}x{v_h} @ {v_fps:.1f} FPS, {frame_cnt} frames)",
                    details={"width": v_w, "height": v_h, "fps": v_fps, "frame_count": frame_cnt},
                ))
            else:
                checks.append(QCCheckItem(
                    category=QCCategory.RENDER_CONTINUITY.value,
                    name="video_stream_integrity",
                    status="FAIL",
                    message=f"Tệp video bị hỏng stream hoặc không giải mã được khung hình (frames={frame_cnt})",
                    details={"is_open": is_open, "frame_count": frame_cnt},
                ))
        except Exception as vid_err:
            checks.append(QCCheckItem(
                category=QCCategory.RENDER_CONTINUITY.value,
                name="video_stream_integrity",
                status="WARN",
                message=f"Không thể probe stream video bằng OpenCV: {vid_err}",
            ))

    # Tổng kết
    pass_cnt = sum(1 for c in checks if c.status == "PASS")
    warn_cnt = sum(1 for c in checks if c.status in ("WARN", "UNKNOWN"))
    fail_cnt = sum(1 for c in checks if c.status == "FAIL")

    if fail_cnt > 0:
        overall = "FAILED"
    elif warn_cnt > 0:
        overall = "PASSED_WITH_WARNINGS"
    else:
        overall = "PASSED"

    report = QCReport(
        job_id=job_id,
        video_name=video_name,
        overall_status=overall,
        policy=effective_policy,
        passed_count=pass_cnt,
        warning_count=warn_cnt,
        failure_count=fail_cnt,
        checks=checks,
    )

    # Lưu báo cáo vào workspace
    base_ws = Path(workspace_path) if workspace_path else Path(__file__).resolve().parent.parent / "workspace"
    qc_dir = base_ws / "bot_system" / "qc_reports"
    qc_dir.mkdir(parents=True, exist_ok=True)
    report_file = qc_dir / f"{job_id}.qc.json"
    try:
        report_file.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[QUALITY_GATE] Đã xuất báo cáo QC cho job {job_id} -> {report_file.name} ({overall})")
    except Exception as save_err:
        logger.warning(f"[QUALITY_GATE] Không thể ghi file báo cáo QC: {save_err}")

    # Subjective/unverified quality stays report-only. Proven corruption or
    # missing/overlapping speech is blocked independently of that UI setting.
    critical = critical_failures(checks)
    if critical:
        raise CriticalQualityError("Critical QC failure (report saved): " + "; ".join(c.message for c in critical))
    # Xử lý policy BLOCK
    if effective_policy == QCPolicy.BLOCK.value and overall == "FAILED":
        fail_msgs = [c.message for c in checks if c.status == "FAIL"]
        raise RuntimeError(f"Quality Gate BLOCK: Job {job_id} thất bại nghiệm thu chất lượng: {'; '.join(fail_msgs)}")

    return report
