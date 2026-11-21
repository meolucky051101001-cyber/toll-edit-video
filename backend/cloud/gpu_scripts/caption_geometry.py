"""Measure source-caption boxes independently of OCR recognition confidence.

The recognizer may be wrong while the text detector still measures a precise
box. Detector agreement at two image scales supplies geometry only; it never
replaces ASR text. An absent/failed detector result is not caption absence.
"""
from __future__ import annotations

import math
from pathlib import Path
import json
from fractions import Fraction
import subprocess


class CaptionGeometryError(ValueError):
    pass


class _CaptionFrameClockError(CaptionGeometryError):
    pass


def _probe_frame_clock(input_video, *, include_frames=False):
    entries = "frame=best_effort_timestamp_time:format=start_time" if include_frames else "stream=r_frame_rate,avg_frame_rate"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-threads", "2", "-select_streams", "v:0",
         *(["-show_frames", "-show_format"] if include_frames else ["-show_streams"]), "-show_entries", entries,
         "-of", "json", str(input_video)],
        capture_output=True, text=True, encoding="utf-8", timeout=120 if include_frames else 15,
        check=True, shell=False,
    )
    return json.loads(probe.stdout)


def _source_has_variable_frame_rate(input_video):
    try:
        stream = _probe_frame_clock(input_video)["streams"][0]
        nominal = Fraction(stream["r_frame_rate"])
        average = Fraction(stream["avg_frame_rate"])
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
        return False
    return nominal > 0 and average > 0 and nominal != average


def _verified_frame_times(input_video, duration):
    try:
        source = _probe_frame_clock(input_video, include_frames=True)
        start = source.get("format", {}).get("start_time", "0")
        origin = 0.0 if start == "N/A" else float(start)
        times = [round(float(frame["best_effort_timestamp_time"]) - origin, 6) for frame in source["frames"]]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        raise _CaptionFrameClockError("FFprobe could not verify source frame timestamps") from exc
    if (not times or any(not math.isfinite(timestamp) or not 0 <= timestamp < duration for timestamp in times)
            or any(right <= left for left, right in zip(times, times[1:]))):
        raise _CaptionFrameClockError("Source presentation timestamps must increase within the video duration")
    return times


def _iou(a, b):
    area = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - area
    return area / union if union > 0 else 0.


def _detect(reader, frame, *, top, bottom, scale):
    import cv2
    height, width = frame.shape[:2]
    y0, y1 = max(0, int(top * height)), min(height, math.ceil(bottom * height))
    crop = frame[y0:y1]
    if not crop.size:
        raise CaptionGeometryError("Empty caption detector crop")
    if scale != 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    horizontal, free = reader.detect(crop, min_size=8, canvas_size=2560, mag_ratio=1.)
    if len(horizontal) != 1 or len(free) != 1:
        raise CaptionGeometryError("Caption detector returned incomplete frame results")
    rectangles = [[b[0], b[2], b[1], b[3]] for b in horizontal[0]]
    rectangles.extend([[min(p[0] for p in b), min(p[1] for p in b),
                        max(p[0] for p in b), max(p[1] for p in b)] for b in free[0]])
    result = []
    for x1, yy1, x2, yy2 in rectangles:
        box = [max(0., float(x1) / scale / width),
               max(0., (float(yy1) / scale + y0) / height),
               min(1., float(x2) / scale / width),
               min(1., (float(yy2) / scale + y0) / height)]
        if not all(math.isfinite(v) for v in box) or box[0] >= box[2] or box[1] >= box[3]:
            raise CaptionGeometryError("Invalid measured caption rectangle")
        result.append(box)
    return result


def _coalesce_same_line(boxes):
    """Merge same-row word/punctuation splits produced by CRAFT spacing gaps."""
    if len(boxes) <= 1:
        return list(boxes)
    ordered = sorted(boxes, key=lambda b: (b[1], b[0]))
    changed = True
    while changed:
        changed = False
        out = []
        used = [False] * len(ordered)
        for i, a in enumerate(ordered):
            if used[i]:
                continue
            cur = list(a)
            used[i] = True
            for j in range(i + 1, len(ordered)):
                if used[j]:
                    continue
                b = ordered[j]
                ha, hb = cur[3] - cur[1], b[3] - b[1]
                v_ov = min(cur[3], b[3]) - max(cur[1], b[1])
                ca_y, cb_y = (cur[1] + cur[3]) * .5, (b[1] + b[3]) * .5
                same_row = (
                    v_ov >= .35 * min(ha, hb)
                    or (cur[1] <= cb_y <= cur[3])
                    or (b[1] <= ca_y <= b[3])
                )
                h_gap = max(cur[0], b[0]) - min(cur[2], b[2])
                max_gap = max(.065, 1.8 * max(ha, hb))
                if same_row and h_gap <= max_gap:
                    cur = [min(cur[0], b[0]), min(cur[1], b[1]), max(cur[2], b[2]), max(cur[3], b[3])]
                    used[j] = True
                    changed = True
            out.append(cur)
        ordered = sorted(out, key=lambda b: (b[1], b[0]))
    return ordered


def _reconcile_two_scales(first, second, *, band_top, band_bottom):
    """Match connected caption clusters across two scales (1-line or 2-line splits)."""
    if not first and not second:
        return []
    band_h = max(.02, band_bottom - band_top)

    def _is_micro_speck(b):
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bw < .065 and (bh < .42 * band_h or bw * bh < .0012):
            return True
        if bh < .42 * band_h and (b[2] < .24 or b[0] > .76) and bw < .16:
            return True
        return False

    def _linked(a, b):
        iw = max(0., min(a[2], b[2]) - max(a[0], b[0]))
        ih = max(0., min(a[3], b[3]) - max(a[1], b[1]))
        inter = iw * ih
        if inter <= 0.:
            return False
        min_area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return (min_area > 0. and inter / min_area >= .35) or _iou(a, b) >= .25

    def _union_box(group):
        return [
            min(b[0] for b in group),
            min(b[1] for b in group),
            max(b[2] for b in group),
            max(b[3] for b in group),
        ]

    n1, n2 = len(first), len(second)
    visited1 = [False] * n1
    visited2 = [False] * n2
    merged = []

    for start_i in range(n1):
        if visited1[start_i]:
            continue
        g1_idx, g2_idx = [start_i], []
        visited1[start_i] = True
        queue = [("1", start_i)]
        while queue:
            side, idx = queue.pop()
            if side == "1":
                for j in range(n2):
                    if not visited2[j] and _linked(first[idx], second[j]):
                        visited2[j] = True
                        g2_idx.append(j)
                        queue.append(("2", j))
            else:
                for i in range(n1):
                    if not visited1[i] and _linked(first[i], second[idx]):
                        visited1[i] = True
                        g1_idx.append(i)
                        queue.append(("1", i))
        if not g2_idx:
            if _is_micro_speck(first[start_i]):
                continue
            return None
        u1 = _union_box([first[i] for i in g1_idx])
        u2 = _union_box([second[j] for j in g2_idx])
        if _iou(u1, u2) < .42:
            if _is_micro_speck(u1) and _is_micro_speck(u2):
                continue
            return None
        merged.append([min(u1[0], u2[0]), min(u1[1], u2[1]), max(u1[2], u2[2]), max(u1[3], u2[3])])

    for j in range(n2):
        if not visited2[j]:
            if _is_micro_speck(second[j]):
                continue
            return None

    return sorted(merged)


def observe_caption_boxes(reader, frame, *, band_top=None, band_bottom=None, x_bounds=None):
    """Return measured boxes, or verified empty; ambiguous detections raise.

    A supported dialogue-caption band is needed to classify detected text as
    captions. Without an anchor, a genuinely text-free frame can still pass.
    """
    anchored = band_top is not None and band_bottom is not None
    if anchored:
        margin = max(.03, (band_bottom - band_top) * .65)
        top, bottom = max(0., band_top - margin), min(1., band_bottom + margin)
    else:
        top, bottom = 0., 1.
    if anchored and x_bounds is not None:
        width = frame.shape[1]
        left, right = int(x_bounds[0] * width), math.ceil(x_bounds[1] * width)
        crop = frame[:, left:right]
        scales = (1.5, 2., 1.75, 2.5)
    else:
        crop = frame
        scales = (1., 1.5, 1.25, 2.)

    def probe(scale):
        boxes = _detect(reader, crop, top=top, bottom=bottom, scale=scale)
        if crop is frame:
            return boxes
        return [[(x1 * (right - left) + left) / width, y1,
                 (x2 * (right - left) + left) / width, y2] for x1, y1, x2, y2 in boxes]

    raw1, raw2 = probe(scales[0]), probe(scales[1])
    if not raw1 and not raw2:
        return []
    if not anchored:
        raise CaptionGeometryError("Detected text has no verified dialogue-caption band")
    def in_band(box):
        box_height = box[3] - box[1]
        if x_bounds is not None and box_height > .055:
            return False  # The verified compact lane contains small caption rows.
        intersection = max(0., min(box[3], band_bottom) - max(box[1], band_top))
        return intersection >= .45 * min(box_height, band_bottom - band_top)
    first = _coalesce_same_line([b for b in raw1 if in_band(b)])
    second = _coalesce_same_line([b for b in raw2 if in_band(b)])
    if not first and not second:
        return []  # Text outside the supported caption band is scene text.
    agreed = _reconcile_two_scales(first, second, band_top=band_top, band_bottom=band_bottom)
    if agreed is not None:
        return agreed
    if reader is not None:
        raw_mid = probe(scales[2])
        mid = _coalesce_same_line([b for b in raw_mid if in_band(b)])
        for cand_a, cand_b in ((first, mid), (mid, second)):
            agreed_mid = _reconcile_two_scales(cand_a, cand_b, band_top=band_top, band_bottom=band_bottom)
            if agreed_mid is not None:
                return agreed_mid

        # A larger sample can recover thin strokes missed in lower-resolution
        # passes. It must corroborate an existing measurement of visible text.
        raw_large = probe(scales[3])
        large = _coalesce_same_line([b for b in raw_large if in_band(b)])
        consensus = []
        for earlier in (first, mid, second):
            measured = _reconcile_two_scales(earlier, large, band_top=band_top, band_bottom=band_bottom)
            if measured:
                consensus.append(measured)
        if consensus:
            agreed = consensus[0]
            for other in consensus[1:]:
                agreed = _reconcile_two_scales(agreed, other, band_top=band_top, band_bottom=band_bottom)
                if not agreed:
                    break
            if agreed:
                return agreed

    raise CaptionGeometryError(
        f"Caption detector scales disagree; geometry needs review (band {band_top:.3f}–{band_bottom:.3f})"
    )


def _make_box_signature_fn(width: int, height: int):
    """Canonicalize minor detector edge jitter without grid-bucket boundary splits."""
    tol_x = max(5.0, float(width) * 0.010)
    tol_y = max(4.0, float(height) * 0.006)
    anchors: list[tuple[tuple[float, float, float, float], ...]] = []

    def _signature(boxes):
        if not boxes:
            return ()
        px_boxes = tuple(
            (b[0] * width, b[1] * height, b[2] * width, b[3] * height)
            for b in boxes
        )
        for anc in anchors:
            if len(anc) != len(px_boxes):
                continue
            if all(
                abs(p[0] - a[0]) <= tol_x
                and abs(p[1] - a[1]) <= tol_y
                and abs(p[2] - a[2]) <= tol_x
                and abs(p[3] - a[3]) <= tol_y
                for p, a in zip(px_boxes, anc)
            ):
                return tuple((round(a[0]), round(a[1]), round(a[2]), round(a[3])) for a in anc)
        anchors.append(px_boxes)
        return tuple((round(p[0]), round(p[1]), round(p[2]), round(p[3])) for p in px_boxes)

    return _signature


def _coalesce_segment_tracking_blocks(rows: list[dict], width: int, height: int) -> list[dict]:
    """Merge immediately touching slices of one cue with micro-jitter into their outer union box."""
    if len(rows) <= 1:
        return list(rows)
    tol_x = max(6.0 / max(1, int(width)), 0.012)
    tol_y = max(5.0 / max(1, int(height)), 0.008)
    by_interval: dict[tuple[float, float], list[dict]] = {}
    raw_bounds: dict[tuple[float, float], tuple[float, float]] = {}
    for row in rows:
        s_val, e_val = float(row["start"]), float(row["end"])
        key = (round(s_val, 6), round(e_val, 6))
        by_interval.setdefault(key, []).append(row)
        if key not in raw_bounds:
            raw_bounds[key] = (s_val, e_val)

    clusters: list[dict] = []
    for key in sorted(by_interval):
        s_val, e_val = raw_bounds[key]
        rects = sorted(by_interval[key], key=lambda r: (float(r["y_pct"]), float(r["x_pct"])))
        if (
            clusters
            and abs(s_val - clusters[-1]["end"]) <= 1e-4
            and len(rects) == len(clusters[-1]["anchor"])
        ):
            within_jitter = all(
                abs(float(r["x_pct"]) - float(a["x_pct"])) <= tol_x
                and abs(float(r["max_x_pct"]) - float(a["max_x_pct"])) <= tol_x
                and abs(float(r["y_pct"]) - float(a["y_pct"])) <= tol_y
                and abs(float(r["max_y_pct"]) - float(a["max_y_pct"])) <= tol_y
                for r, a in zip(rects, clusters[-1]["anchor"])
            )
            if within_jitter:
                clusters[-1]["end"] = e_val
                clusters[-1]["union"] = [
                    {
                        **u,
                        "x_pct": min(float(u["x_pct"]), float(r["x_pct"])),
                        "y_pct": min(float(u["y_pct"]), float(r["y_pct"])),
                        "max_x_pct": max(float(u["max_x_pct"]), float(r["max_x_pct"])),
                        "max_y_pct": max(float(u["max_y_pct"]), float(r["max_y_pct"])),
                    }
                    for u, r in zip(clusters[-1]["union"], rects)
                ]
                continue
        clusters.append({
            "start": s_val,
            "end": e_val,
            "anchor": rects,
            "union": [dict(r) for r in rects],
        })

    coalesced: list[dict] = []
    for cluster in clusters:
        c_start, c_end = cluster["start"], cluster["end"]
        for u in cluster["union"]:
            coalesced.append({**u, "start": c_start, "end": c_end})
    return coalesced


def acquire_caption_geometry(input_video, segments, reader, *, band_top=None, band_bottom=None,
                             x_bounds=None, duration, audit_path, sample_step=.35, max_samples=4000,
                             max_extra_frames=2000):
    """Reacquire source geometry across the timeline, including speech gaps.

    Changed sampled boxes are refined to adjacent decoded frames. Identical
    coarse samples keep the sampling contract; this is not exhaustive OCR.
    All boxes remain measured; neither ASR confidence nor a guessed band is a mask.
    """
    import cv2
    from visual_caption_timing import refine_observations, observation_intervals
    capture = cv2.VideoCapture(str(input_video))
    if not capture.isOpened():
        raise CaptionGeometryError("Cannot open source for caption geometry")
    report = {"version": 1, "passed": False, "policy": "two_scale_detector_source_timeline",
              "sample_step_seconds": sample_step, "cue_count": len(segments)}
    try:
        dimensions = [float(capture.get(prop)) for prop in
                      (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
                       cv2.CAP_PROP_FPS, cv2.CAP_PROP_FRAME_COUNT)]
        if any(not math.isfinite(value) or value <= 0 for value in dimensions):
            raise CaptionGeometryError("Invalid source frame clock")
        width, height, fps, count = dimensions
        width, height, count = int(width), int(height), int(count)
        if min(width, height, count) <= 0:
            raise CaptionGeometryError("Invalid source frame clock")
        def rewind():
            nonlocal capture
            capture.release()
            capture = cv2.VideoCapture(str(input_video))
            if not capture.isOpened():
                raise _CaptionFrameClockError("Cannot reopen the source for sequential decoding")

        def collect_samples(frame_times=None):
            verified_clock = frame_times is not None
            frame_count = len(frame_times) if verified_clock else count
            step = max(1, int(fps * sample_step))
            indices = sorted(set(range(0, frame_count, step)) | {frame_count - 1})
            if len(indices) > max_samples:
                raise CaptionGeometryError("Caption geometry exceeds its sampling budget")
            times = {}
            can_step = callable(getattr(capture, "grab", None))
            next_index = 0 if verified_clock else None

            def observe(index):
                nonlocal next_index
                if verified_clock and index < next_index:
                    rewind()
                    next_index = 0
                if verified_clock or (can_step and next_index is not None and 0 <= index - next_index <= max(24, step + 4)):
                    for _ in range(index - next_index):
                        if not capture.grab():
                            raise _CaptionFrameClockError(f"Source frame step failed before {index}")
                elif not capture.set(cv2.CAP_PROP_POS_FRAMES, index):
                    raise _CaptionFrameClockError(f"Source frame seek failed at {index}")
                ok, frame = capture.read()
                next_frame = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
                if not ok or frame is None or not math.isfinite(next_frame) or abs(next_frame - (index + 1)) > .01:
                    raise _CaptionFrameClockError(f"Decoded frame does not match requested frame {index}")
                next_index = index + 1
                pts = frame_times[index] if verified_clock else float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.
                if not math.isfinite(pts) or not 0 <= pts < duration:
                    raise _CaptionFrameClockError(f"Source frame {index} could not be verified")
                times[index] = pts
                try:
                    return observe_caption_boxes(reader, frame, band_top=band_top, band_bottom=band_bottom,
                                                 x_bounds=x_bounds)
                except CaptionGeometryError as exc:
                    raise CaptionGeometryError(f"Frame {index} at {pts:.3f}s: {exc}") from exc

            samples = {index: observe(index) for index in indices}
            if verified_clock and capture.grab():
                raise _CaptionFrameClockError("Decoded frame count does not match the source timeline")
            signature = _make_box_signature_fn(width, height)
            samples, extra = refine_observations(
                samples, frame_count, observe, signature, max_extra_frames=max_extra_frames,
                observe_batch=lambda batch: [observe(index) for index in batch],
            )
            ordered = sorted(samples)
            if any(times[b] <= times[a] for a, b in zip(ordered, ordered[1:])):
                raise _CaptionFrameClockError("Decoded caption timestamps are not strictly increasing")
            return samples, times, ordered, extra, signature

        if _source_has_variable_frame_rate(input_video):
            report["frame_clock"] = "source_presentation_timestamps"
            collected = collect_samples(_verified_frame_times(input_video, duration))
        else:
            report["frame_clock"] = "opencv"
            try:
                collected = collect_samples()
            except _CaptionFrameClockError:
                rewind()
                report["frame_clock"] = "source_presentation_timestamps"
                collected = collect_samples(_verified_frame_times(input_video, duration))
        samples, times, ordered, extra, signature = collected
        blocks = []
        for position, (boxes, start, end) in enumerate(observation_intervals(samples, fps, duration, frame_times=times)):
            neighbour = samples[ordered[position + 1]] if position + 1 < len(ordered) else boxes
            if signature(boxes) == signature(neighbour) and len(boxes) == len(neighbour):
                boxes = [[min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])]
                         for a,b in zip(boxes,neighbour)]
            for box in boxes:
                blocks.append(dict(start=start, end=end, x_pct=box[0], y_pct=box[1],
                                   max_x_pct=box[2], max_y_pct=box[3],
                                   geometry_method="two_scale_text_detector", is_subtitle=True,
                                   in_subtitle_band=True, is_packaging=False, type="subtitle"))
        for segment in segments:
            segment["tracking_blocks"] = []
            segment["best_block"] = None
        for block in blocks:
            if not segments:
                raise CaptionGeometryError("Visible captions have no transcript owner")
            middle = (block["start"] + block["end"]) / 2
            owner = min(segments, key=lambda s: max(float(s["start"])-middle, middle-float(s["end"]), 0.))
            owner["tracking_blocks"].append(block)
        total_coalesced_masks = 0
        for segment in segments:
            rows = _coalesce_segment_tracking_blocks(segment["tracking_blocks"], width, height)
            segment["tracking_blocks"] = rows
            total_coalesced_masks += len(rows)
            segment["best_block"] = dict(rows[0]) if rows else None
            segment["ocr_mask_status"] = "located" if rows else "verified_no_caption"
            segment["caption_geometry_verified"] = True
        report.update(passed=True, sampled_frames=len(samples), refinement_frames=extra,
                      caption_samples=sum(bool(v) for v in samples.values()),
                      empty_samples=sum(not v for v in samples.values()),
                      source_mask_count=total_coalesced_masks,
                      verified_cues=[str(s["id"]) for s in segments],
                      unresolved_cues=[], observations=[{"time":times[i],"boxes":samples[i]} for i in ordered])
        return report
    except Exception as exc:
        report["error"] = str(exc)
        raise CaptionGeometryError(str(exc)) from exc
    finally:
        capture.release()
        Path(audit_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_caption_evidence(evidence, *, cue_count):
    """Shared producer/consumer gate; legacy absence cannot become a PASS."""
    if not isinstance(evidence, dict) or evidence.get("version") != 1 or evidence.get("passed") is not True:
        raise CaptionGeometryError("Source-caption geometry has not been verified")
    ids = evidence.get("verified_cues")
    if (not isinstance(ids, list) or len(ids) != cue_count or len(set(map(str, ids))) != cue_count
            or evidence.get("cue_count") != cue_count or evidence.get("unresolved_cues") != []):
        raise CaptionGeometryError("Source-caption evidence does not cover every cue")
    total = evidence.get("sampled_frames")
    captions, empty = evidence.get("caption_samples"), evidence.get("empty_samples")
    if any(type(n) is not int or n < 0 for n in (total, captions, empty)) or total <= 0 or captions + empty != total:
        raise CaptionGeometryError("Source-caption observations are incomplete")
    if captions and (type(evidence.get("source_mask_count")) is not int or evidence["source_mask_count"] <= 0):
        raise CaptionGeometryError("Detected source captions have no masks")
