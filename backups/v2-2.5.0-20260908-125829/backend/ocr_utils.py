import cv2
import logging
import os
import tempfile
from pathlib import Path

from ai.model_policy import current_model_policy
from ai.model_runtime import ModelRuntimeError, run_model_stage, runtime_module_available

try:
    from .ocr_subtitle_locator import select_chinese_subtitle_band
except ImportError:  # Running ocr_utils.py from backend/ on Windows.
    from ocr_subtitle_locator import select_chinese_subtitle_band

logger = logging.getLogger(__name__)
reader = None

def get_ocr_reader():
    global reader
    if reader is None:
        import easyocr

        logger.info("Initializing EasyOCR reader (GPU=True)...")
        # Gỡ bỏ 'en' để tránh EasyOCR bị ảo giác (nhận diện nhầm nhiễu thành chữ tiếng Anh)
        reader = easyocr.Reader(['ch_sim'], gpu=os.getenv('OCR_GPU', '1') != '0')
    return reader

def release_ocr_reader():
    global reader
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
    """Recognize all sampled frames in one PP-OCRv6 process, then fallback safely."""

    policy = current_model_policy()
    if policy.ocr_backend in {"auto", "paddle"} and runtime_module_available(
        "paddleocr", policy
    ):
        try:
            with tempfile.TemporaryDirectory(prefix="autodub-ocr-") as temporary:
                paths = []
                for index, frame in enumerate(frames):
                    path = Path(temporary) / "frame_{:04d}.png".format(index)
                    if not cv2.imwrite(str(path), frame):
                        raise OSError("Could not write OCR frame {}".format(path))
                    paths.append(str(path))
                result = run_model_stage(
                    "paddle_ocr",
                    {
                        "images": paths,
                        "ocr_version": policy.paddle_ocr_version,
                        "engine": policy.paddle_ocr_engine,
                    },
                    timeout_seconds=float(os.getenv("OCR_MODEL_TIMEOUT_SECONDS", "1800")),
                    policy=policy,
                )
                by_path = {
                    str(item.get("path")): item.get("rows", [])
                    for item in result.get("images", [])
                }
                if any(path not in by_path for path in paths):
                    raise RuntimeError("OCR response omitted a frame")
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
                    "PP-OCRv6 completed %d sampled frames via %s",
                    len(frames),
                    policy.paddle_ocr_engine,
                )
                return output
        except (ModelRuntimeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("PP-OCRv6 failed; falling back to EasyOCR: %s", exc)

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
    if kwargs.get('end_time') is not None:
        duration = min(duration, float(kwargs['end_time'])) if duration > 0 else float(kwargs['end_time'])
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
        for seg_idx, seg in enumerate(srt_segments):
            s = seg.start.total_seconds()
            e = seg.end.total_seconds()
            if e <= s:
                continue
            # Fixed temporal cadence rather than three snapshots per long ASR cue.
            interval = max(0.08, min(0.5, float(os.getenv("OCR_TRACK_INTERVAL", "0.2"))))
            # Visual subtitles can lead/lag ASR boundaries. Sample a bounded
            # overlap using this cue's transcript, never unrelated scene text.
            s = max(0.0, s - 0.3)
            # Include silent gaps: burnt-in captions often outlive the speech.
            next_start = (srt_segments[seg_idx + 1].start.total_seconds()
                          if seg_idx + 1 < len(srt_segments) else duration)
            e = max(e, next_start) + 0.3
            if duration > 0:
                e = min(e, duration)
            count = max(1, __import__("math").ceil((e - s) / interval))
            for n in range(count):
                target_timestamps.append((s + (n + 0.5) * (e - s) / count, seg, seg_idx))
    else:
        # Without a transcript there is no reliable way to distinguish scene text.
        cap.release()
        return [], width, height, 0.85

    crop_y_start = int(height * 0.05) # Quét từ 5% (bỏ thanh trạng thái)
    crop_y_end = int(height * 0.95)   # đến 95%
    captured_frames = []
    recognized_samples = []
    frame_cache = None
    ocr_frame_count = 0
    last_full_probe = -10.0

    def capture(current_time):
        cap.set(cv2.CAP_PROP_POS_MSEC, current_time * 1000)
        ret, frame = cap.read()
        if not ret:
            return None
        crop = frame[crop_y_start:crop_y_end, :]
        ratio = min(1.0, 720.0 / crop.shape[1])
        if ratio < 1:
            crop = cv2.resize(crop, (720, int(crop.shape[0]*ratio)), interpolation=cv2.INTER_AREA)
        return crop, ratio

    # Six distributed probes establish a band before any dense recognition.
    # The cheap image comparison still checks every tracking timestamp.
    if kwargs.get("adaptive", True) and hasattr(cv2, "Canny") and target_timestamps:
        from ocr_frame_cache import SubtitleFrameCache
        seed_indices = sorted({round(i*(len(target_timestamps)-2)/5) for i in range(6)})
        seeds = []
        for index in seed_indices:
            t, seg, idx = target_timestamps[index]
            captured = capture(t)
            if captured is not None:
                frame, ratio = captured
                seeds.append((t, seg, idx, frame, ratio))
        try:
            seed_rows = _readtext_batch([s[3] for s in seeds]) if seeds else []
        except BaseException:
            cap.release()
            raise
        if len(seed_rows) != len(seeds):
            cap.release()
            raise RuntimeError("OCR returned incomplete probe results")
        ocr_frame_count += len(seeds)
        probe_blocks, texts = [], {}
        for (t, seg, idx, frame, ratio), rows in zip(seeds, seed_rows):
            sid = getattr(seg, "index", idx)
            texts[sid] = seg.content
            for bbox, text, prob in rows:
                if len(bbox) < 4:
                    continue
                probe_blocks.append(dict(text=text, prob=prob, sample_time=t,
                    sample_segment_id=sid,
                    x_pct=min(p[0] for p in bbox)/ratio/width,
                    max_x_pct=max(p[0] for p in bbox)/ratio/width,
                    y_pct=(min(p[1] for p in bbox)/ratio+crop_y_start)/height,
                    max_y_pct=(max(p[1] for p in bbox)/ratio+crop_y_start)/height))
        probe_band = select_chinese_subtitle_band(probe_blocks, texts, width, height)
        cache_top, cache_bottom = probe_band.top, probe_band.bottom
        cache_region_found = probe_band.support >= 1
        if not cache_region_found:
            # Spatial agreement only chooses a region to compare/cache. It does
            # not authorize any mask; final selection still requires ASR anchors.
            lines = [b for b in probe_blocks
                     if len(b['text']) >= 4 and b['max_x_pct']-b['x_pct'] >= .25
                     and .012 <= b['max_y_pct']-b['y_pct'] <= .10
                     and abs((b['x_pct']+b['max_x_pct'])/2-.5) <= .10]
            clusters = [[b for b in lines if abs(b['y_pct']-a['y_pct']) <= .025
                         and abs(b['max_y_pct']-a['max_y_pct']) <= .025] for a in lines]
            cluster = max(clusters, key=len, default=[])
            if len({b['sample_time'] for b in cluster}) >= 3:
                cache_top = min(b['y_pct'] for b in cluster)
                cache_bottom = max(b['max_y_pct'] for b in cluster)
                cache_region_found = True
        if cache_region_found:
            # Padding notices nearby second lines or small vertical movement.
            top = max(0, (cache_top*height-crop_y_start)/ (crop_y_end-crop_y_start)-.02)
            bottom = min(1, (cache_bottom*height-crop_y_start)/(crop_y_end-crop_y_start)+.02)
            frame_cache = SubtitleFrameCache(top, bottom)
        logger.info("OCR probes=%d, band_support=%d, visual reuse=%s", len(seeds), probe_band.support, frame_cache is not None)

    def flush_frames():
        nonlocal ocr_frame_count
        if not captured_frames:
            return
        images, offsets = [], []
        for item in captured_frames:
            frame, holder = item[2], item[5]
            if frame_cache and not holder['full_probe']:
                offset = max(0, int(frame.shape[0]*frame_cache.top))
                image = frame[offset:min(frame.shape[0], int(frame.shape[0]*frame_cache.bottom))]
            else:
                image, offset = frame, 0
            images.append(image)
            offsets.append(offset)
        results = _readtext_batch(images)
        ocr_frame_count += len(captured_frames)
        if len(results) != len(captured_frames):
            raise RuntimeError("OCR returned incomplete frame results")
        for item, rows, offset in zip(captured_frames, results, offsets):
            current_time, scale_ratio, frame, target_seg, seg_idx, holder = item
            holder["rows"] = [([[p[0], p[1]+offset] for p in box], text, prob)
                              for box, text, prob in rows]
            recognized_samples.append((current_time, scale_ratio, target_seg, seg_idx, holder))
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

            signature = frame_cache.signature(proc_frame) if frame_cache else None
            full_probe = current_time-last_full_probe >= 1.0
            reused = frame_cache.lookup(signature, current_time) if frame_cache and not full_probe else None
            if reused is not None:
                # Reuse only rows inside the visually checked region. Product
                # text elsewhere in a seed frame may have moved since then.
                recognized_samples.append((current_time, scale_ratio, target_seg, seg_idx,
                                           {"reuse": reused, "shape_h": proc_frame.shape[0]}))
                continue
            holder = {'full_probe': full_probe}
            if full_probe:
                last_full_probe = current_time
            captured_frames.append((current_time, scale_ratio, proc_frame, target_seg, seg_idx, holder))
            if frame_cache:
                frame_cache.remember(signature, current_time, holder)
            if len(captured_frames) >= (32 if frame_cache else 12):
                flush_frames()

    finally:
        cap.release()
    flush_frames()
    logger.info("OCR recognized=%d, reused=%d, tracking_samples=%d", ocr_frame_count,
                frame_cache.hits if frame_cache else 0, len(recognized_samples))
    for current_time, scale_ratio, target_seg, seg_idx, holder in recognized_samples:
        results = holder.get("rows", [])
        if "reuse" in holder:
            results = [row for row in holder["reuse"]["rows"]
                       if min(p[1] for p in row[0]) >= frame_cache.top*holder["shape_h"]
                       and max(p[1] for p in row[0]) <= frame_cache.bottom*holder["shape_h"]]
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
                seg_e = max(seg.end.total_seconds(), next_start) + 0.3
                if duration > 0:
                    seg_e = min(seg_e, duration)

                if b:
                    selected = stabilize_samples(band.selected_by_sample.get(s_key, []))
                    tracking = []
                    for position, row in enumerate(selected):
                        left = max(seg_s, row["sample_time"] - interval / 2) if position == 0 else max(row["sample_time"] - interval / 2, (
                            selected[position - 1]["sample_time"] + row["sample_time"]
                        ) / 2.0)
                        previous = selected[position - 1] if position else None
                        same_caption = previous is not None and (
                            abs(row["y_pct"] - previous["y_pct"]) <= .012
                            and abs(row["max_y_pct"] - previous["max_y_pct"]) <= .012)
                        if position and row["sample_time"] - previous["sample_time"] <= (.80001 if same_caption else .40001):
                            # Bridge a single missed sample, not a real long absence.
                            left = (selected[position - 1]["sample_time"] + row["sample_time"]) / 2
                        # Never stretch a detection to the end of a long ASR cue.
                        # Last confirmed presence expires within 0.4s, leaving
                        # headroom below the requested 0.5s disappearance limit.
                        right = min(seg_e, row["sample_time"] + 0.4) if position == len(selected) - 1 else min(row["sample_time"] + 0.4, (
                            row["sample_time"] + selected[position + 1]["sample_time"]
                        ) / 2.0)
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
