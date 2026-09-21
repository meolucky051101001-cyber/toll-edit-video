import cv2
import logging
import os
import tempfile
import hashlib
import copy
from collections import OrderedDict
from pathlib import Path
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

def release_ocr_reader():
    global reader, _paddle_failed
    _paddle_failed = False
    _frame_cache.clear()
    try:
        from .ai.v1_model_runtime import close_session
    except ImportError:
        from ai.v1_model_runtime import close_session
    close_session()
    if reader is not None:
        logger.info("🧹 Đang giải phóng bộ nhớ RAM/VRAM của EasyOCR...")
        del reader
        reader = None
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


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
    if not _paddle_failed and policy.ocr_backend in {"auto", "paddle"} and runtime_module_available(
        "paddleocr", policy
    ):
        try:
            with tempfile.TemporaryDirectory(prefix="autodub-v1-ocr-frames-") as temporary:
                paths = []
                for index, frame in enumerate(frames):
                    path = Path(temporary) / "frame_{:04d}.png".format(index)
                    if not cv2.imwrite(str(path), frame):
                        raise OSError("Could not write OCR frame {}".format(path))
                    paths.append(str(path))

                result = run_v1_ocr(
                    {
                        "images": paths,
                        "detection_model": policy.paddle_detection_model,
                        "recognition_model": policy.paddle_recognition_model,
                        "engine": policy.paddle_engine,
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
            try:
                from .ai.v1_model_runtime import close_session
            except ImportError:
                from ai.v1_model_runtime import close_session
            close_session()
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
        compatible = bool(group) and row["sample_time"] - group[-1]["sample_time"] <= .40001
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


@stage("ocr", cleanup=release_ocr_reader)
@ocr_proxy
def perform_video_ocr(video_path, target_lang='vi', sample_rate=1.0, api_key=None, srt_segments=None, **kwargs):
    logger.info(f"Bắt đầu OCR toàn diện trên video {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [], 1080, 1920, 0.85

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps < 1: fps = 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count_key = getattr(cv2, "CAP_PROP_FRAME_COUNT", None)
    duration = cap.get(frame_count_key) / fps if frame_count_key is not None else 0
    if width <= 0 or height <= 0:
        cap.release()
        raise ValueError("Video has invalid dimensions")

    class OCRBlock:
        def __init__(self, text, start, end, x_pct, max_x_pct, y_pct, max_y_pct, prob=1.0, sample_segment_id=None, sample_time=0.0):
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

    all_blocks = []

    # === TỐI ƯU HÓA SIÊU TỐC OCR THEO TỪNG ĐOẠN THOẠI ===
    target_timestamps = []
    if srt_segments:
        # Cover every speech segment; process recognition in bounded batches.
        interval = max(0.1, min(5.0, float(os.getenv("V1_OCR_TRACK_INTERVAL", "1.2"))))
        max_samples_per_cue = max(1, int(os.getenv("V1_OCR_MAX_SAMPLES_PER_SEGMENT", "2")))
        for seg_idx, seg in enumerate(srt_segments):
            s = seg.start.total_seconds()
            e = seg.end.total_seconds()
            if e <= s:
                continue
            s = max(0.0, s - 0.3)
            next_start = (srt_segments[seg_idx + 1].start.total_seconds()
                          if seg_idx + 1 < len(srt_segments) else duration)
            e = min(e + 0.4, next_start) if next_start > e else (e + 0.3)
            if duration > 0:
                e = min(e, duration)
            cue_dur = max(0.1, e - s)
            count = min(max_samples_per_cue, max(1, __import__("math").ceil(cue_dur / interval)))
            for n in range(count):
                target_timestamps.append((s + (n + 0.5) * cue_dur / count, seg, seg_idx))
    else:
        # Without a transcript there is no reliable way to distinguish scene text.
        cap.release()
        return [], width, height, 0.85

    crop_y_start = int(height * 0.05) # Quét từ 5% (bỏ thanh trạng thái)
    crop_y_end = int(height * 0.95)   # đến 95%
    captured_frames = []
    recognized_samples = []

    def flush_frames():
        if not captured_frames:
            return
        results = _readtext_batch([item[2] for item in captured_frames])
        if len(results) != len(captured_frames):
            raise RuntimeError("OCR returned incomplete frame results")
        for item, rows in zip(captured_frames, results):
            current_time, scale_ratio, _, target_seg, seg_idx = item
            recognized_samples.append((current_time, scale_ratio, target_seg, seg_idx, rows))
        captured_frames.clear()

    try:
        for current_time, target_seg, seg_idx in target_timestamps:
            try:
                from . import shared_state
            except ImportError:
                import shared_state
            if getattr(shared_state, 'stop_requested', False):
                cap.release()
                return [], width, height, 0.85

            cap.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000)
            ret, frame = cap.read()
            if not ret: continue

            # 1. Cắt vùng chứa phụ đề tiềm năng (từ 5% đến 95% chiều cao màn hình)
            cropped_frame = frame[crop_y_start:crop_y_end, :]

            # 2. Resize nhanh về độ phân giải chuẩn 720p để EasyOCR tăng tốc gấp 3 lần nhưng vẫn siêu nét
            orig_crop_h, orig_crop_w = cropped_frame.shape[:2]
            scale_ratio = 1.0
            if orig_crop_w > 720:
                scale_ratio = 720.0 / orig_crop_w
                target_w = 720
                target_h = int(orig_crop_h * scale_ratio)
                proc_frame = cv2.resize(cropped_frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
            else:
                proc_frame = cropped_frame

            captured_frames.append((current_time, scale_ratio, proc_frame, target_seg, seg_idx))
            if len(captured_frames) >= 12:
                flush_frames()

    finally:
        cap.release()
    flush_frames()
    for current_time, scale_ratio, target_seg, seg_idx, results in recognized_samples:
        frame_blocks = []
        for (bbox, text, prob) in results:
            clean_t = str(text or "").strip()
            # Khong bo qua chu Han ke ca khi prob thap vi font chu nghe thuat co vien thuong co prob = 0.00
            if len(clean_t) == 0 or len(bbox) < 4:
                continue

            xs = [pt[0] / scale_ratio for pt in bbox]
            ys = [(pt[1] / scale_ratio) + crop_y_start for pt in bbox] # Bù lại vị trí cắt dọc

            x1, x2 = min(xs), max(xs)
            y1, y2 = min(ys), max(ys)

            x1 = max(0, x1 - int(width * 0.01))
            x2 = min(width, x2 + int(width * 0.01))
            y1 = max(0, y1 - int(height * 0.005))
            y2 = min(height, y2 + int(height * 0.005))

            x_pct = x1 / width
            max_x_pct = x2 / width
            y_pct = y1 / height
            max_y_pct = y2 / height

            frame_blocks.append({
                'text': clean_t, 'x_pct': x_pct, 'max_x_pct': max_x_pct,
                'y_pct': y_pct, 'max_y_pct': max_y_pct, 'prob': prob
            })

        merged_frame_blocks = frame_blocks

        s_id = getattr(target_seg, 'index', None) if target_seg is not None else seg_idx
        sample_id = s_id if s_id is not None else seg_idx
        for mb in merged_frame_blocks:
            all_blocks.append(OCRBlock(
                text=mb['text'],
                start=current_time - 1.2,
                end=current_time + 1.2,
                x_pct=mb['x_pct'],
                max_x_pct=mb['max_x_pct'],
                y_pct=mb['y_pct'],
                max_y_pct=mb['max_y_pct'],
                prob=mb.get('prob', 1.0),
                sample_segment_id=sample_id,
                sample_time=current_time,
            ))

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

    band = select_chinese_subtitle_band(
        blocks=all_blocks,
        segment_texts=segment_texts,
        frame_width=width,
        frame_height=height,
        default_top=0.75,
        default_bottom=0.82,
    )

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
                    for position, row in enumerate(selected):
                        left = seg_s if position == 0 else (selected[position - 1]["sample_time"] + row["sample_time"]) / 2.0
                        right = seg_e if position == len(selected) - 1 else (row["sample_time"] + selected[position + 1]["sample_time"]) / 2.0
                        if right > left:
                            tracking.append(OCRBlock(
                                **{key: row[key] for key in
                                   ("text", "x_pct", "max_x_pct", "y_pct", "max_y_pct")},
                                start=max(seg_s, left), end=min(seg_e, right),
                                prob=row.get("prob", 0.0),
                                sample_segment_id=s_key, sample_time=row["sample_time"],
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
                    )
                    seg.y_pct = b["y_pct"]
                    seg.max_y_pct = b["max_y_pct"]
                    logger.info(f"Sync (Subtitle Band): '{str(getattr(seg, 'content', ''))[:15]}' -> Y: {seg.y_pct:.3f} - {seg.max_y_pct:.3f}")
                else:
                    # No subtitle found for this segment: do NOT assign random Chinese block!
                    seg.best_block = None
                    seg.tracking_blocks = []
                    seg.y_pct = global_med_top
                    seg.max_y_pct = global_med_bottom

        # BỔ SUNG CÂU THOẠI TỪ OCR (Nếu ASR bị nhạc to át mất câu)
        try:
            recovered_subs = recover_missing_subtitles_from_ocr(
                all_blocks=all_blocks,
                srt_segments=srt_segments,
                band=band,
                duration=duration,
            )
            if recovered_subs:
                logger.info(f"🎯 OCR Fallback phát hiện {len(recovered_subs)} câu phụ đề bị ASR bỏ sót: {[r['text'] for r in recovered_subs]}")
                import srt
                from datetime import timedelta
                for rec in recovered_subs:
                    new_seg = srt.Subtitle(
                        index=len(srt_segments) + 1,
                        start=timedelta(seconds=rec["start"]),
                        end=timedelta(seconds=rec["end"]),
                        content=rec["text"],
                    )
                    b = rec["best_block"]
                    new_seg.best_block = OCRBlock(
                        text=getattr(b, "text", "") if hasattr(b, "text") else b.get("text", ""),
                        start=rec["start"],
                        end=rec["end"],
                        x_pct=getattr(b, "x_pct", 0.0) if hasattr(b, "x_pct") else b.get("x_pct", 0.0),
                        max_x_pct=getattr(b, "max_x_pct", 1.0) if hasattr(b, "max_x_pct") else b.get("max_x_pct", 1.0),
                        y_pct=getattr(b, "y_pct", global_med_top) if hasattr(b, "y_pct") else b.get("y_pct", global_med_top),
                        max_y_pct=getattr(b, "max_y_pct", global_med_bottom) if hasattr(b, "max_y_pct") else b.get("max_y_pct", global_med_bottom),
                        prob=getattr(b, "prob", 1.0) if hasattr(b, "prob") else b.get("prob", 1.0),
                    )
                    new_seg.y_pct = new_seg.best_block.y_pct
                    new_seg.max_y_pct = new_seg.best_block.max_y_pct
                    new_seg.tracking_blocks = []
                    srt_segments.append(new_seg)

                # Sort by start time and re-index
                srt_segments.sort(key=lambda s: s.start)
                for idx, s in enumerate(srt_segments, start=1):
                    s.index = idx
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

    return [], width, height, main_y_pct


def recover_missing_subtitles_from_ocr(all_blocks, srt_segments, band, duration=None):
    """
    Recover missing speech segments from reliable OCR blocks in the Chinese Subtitle Band
    that were missed by Whisper ASR (e.g. drowned out by loud music).
    """
    if not all_blocks or getattr(band, "support", 0) <= 0 or getattr(band, "mode", "") == "default":
        return []

    # 1. Collect existing segment intervals
    existing_intervals = []
    if srt_segments:
        for seg in srt_segments:
            existing_intervals.append((seg.start.total_seconds() - 0.5, seg.end.total_seconds() + 0.5))

    # 2. Filter candidate blocks in subtitle band
    candidates = []
    for b in all_blocks:
        def value(name, default=None):
            return b.get(name, default) if isinstance(b, dict) else getattr(b, name, default)
        if (value('is_packaging') is True or value('is_static') is True
                or value('is_subtitle') is False or value('in_subtitle_band') is False
                or str(value('type', '')).lower() in ('packaging', 'logo', 'watermark', 'background')):
            continue
        prob = float(getattr(b, "prob", 1.0) if hasattr(b, "prob") else b.get("prob", 1.0))
        text = str(getattr(b, "text", "") if hasattr(b, "text") else b.get("text", "")).strip()
        y_pct = float(getattr(b, "y_pct", 0.0) if hasattr(b, "y_pct") else b.get("y_pct", 0.0))
        sample_time = float(getattr(b, "sample_time", 0.0) if hasattr(b, "sample_time") else b.get("sample_time", 0.0))

        # Check band alignment
        if not (band.top - 0.035 <= y_pct <= band.bottom + 0.035):
            continue
        # Check text quality: at least 2 Chinese characters
        ch_chars = [c for c in text if "\u3400" <= c <= "\u9fff"]
        if prob < 0.60 or len(ch_chars) < 2:
            continue
        # Check if already covered by ASR
        covered = any(st <= sample_time <= et for st, et in existing_intervals)
        if covered:
            continue
        candidates.append(b)

    if not candidates:
        return []

    # 3. Group consecutive frames of the same subtitle
    def get_st(blk):
        return float(getattr(blk, "sample_time", 0.0) if hasattr(blk, "sample_time") else blk.get("sample_time", 0.0))

    candidates.sort(key=get_st)
    clusters = []
    current_cluster = [candidates[0]]
    for b in candidates[1:]:
        prev_time = get_st(current_cluster[-1])
        from difflib import SequenceMatcher
        def caption_text(item):
            return str(item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')).strip()
        same_caption = SequenceMatcher(None, caption_text(current_cluster[0]), caption_text(b), autojunk=False).ratio() >= 0.8
        if get_st(b) - prev_time <= 1.5 and same_caption:
            current_cluster.append(b)
        else:
            clusters.append(current_cluster)
            current_cluster = [b]
    if current_cluster:
        clusters.append(current_cluster)

    # 4. Convert clusters to recovered Subtitle segments
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

        def get_prob_len(blk):
            p = float(getattr(blk, "prob", 0.0) if hasattr(blk, "prob") else blk.get("prob", 0.0))
            t = str(getattr(blk, "text", "") if hasattr(blk, "text") else blk.get("text", ""))
            return (p, len(t))

        best_block = max(cluster, key=get_prob_len)
        text = str(getattr(best_block, "text", "") if hasattr(best_block, "text") else best_block.get("text", "")).strip()
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
