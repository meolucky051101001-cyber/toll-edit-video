"""Bounded per-cue coarse OCR followed by visual verification/refinement.

Two ordered passes over each cue replace repeated random seeks. Coarse results
are reused only for visually equivalent frames; ASR timing alone never proves
that a subtitle is present. No video-sized image collection is retained.
"""
import math
import time

import cv2

try:
    from .ocr_frame_cache import SubtitleFrameCache
    from .ocr_subtitle_locator import select_chinese_subtitle_band
except ImportError:
    from ocr_frame_cache import SubtitleFrameCache
    from ocr_subtitle_locator import select_chinese_subtitle_band


class OrderedFrameReader:
    def __init__(self, capture, fps, width, height, stopped, metrics, duration=0):
        self.cap, self.fps = capture, fps
        self.width, self.height = width, height
        self.stopped, self.metrics = stopped, metrics
        self.next_index = None
        self.last_index, self.last_crop = None, None
        self.last_valid_index = max(0, math.ceil(duration * fps) - 1) if duration > 0 else None

    def read(self, timestamp):
        if self.stopped():
            raise RuntimeError("OCR cancelled")
        index = max(0, int(round(timestamp * self.fps)))
        if self.last_valid_index is not None:
            index = min(index, self.last_valid_index)
        if index == self.last_index:
            return self.last_crop
        if (self.next_index is None or index < self.next_index
                or index - self.next_index > self.fps * 2
                or not hasattr(self.cap, "grab")):
            # Seek at cue boundaries or long empty gaps, never once per sample.
            self.cap.set(cv2.CAP_PROP_POS_MSEC, index * 1000 / self.fps)
            self.metrics["seeks"] += 1
            self.next_index = index
        while self.next_index < index:
            if self.stopped():
                raise RuntimeError("OCR cancelled")
            if not self.cap.grab():
                raise RuntimeError("Could not decode OCR verification frame")
            self.next_index += 1
            self.metrics["grabbed_frames"] += 1
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Could not decode OCR verification frame")
        self.next_index = index + 1
        self.metrics["decoded_samples"] += 1
        crop = frame[int(self.height * .05):int(self.height * .95), :]
        ratio = min(1.0, 720.0 / crop.shape[1])
        if ratio < 1:
            crop = cv2.resize(crop, (720, int(crop.shape[0] * ratio)),
                              interpolation=cv2.INTER_AREA)
        self.last_index, self.last_crop = index, (crop, ratio)
        return self.last_crop


def collect_adaptive_samples(cap, segments, width, height, fps, duration,
                             recognize, stopped, metrics):
    """Return legacy-compatible samples, using at most twelve pending images.

    Short, ASR-validated cues use cheap visual checks every 0.2s. Unmatched
    cues refine every 0.5s. Cues over 3s periodically refresh OCR even if the
    image is stable. A visual change always invalidates reuse, including a
    missing line or a move outside the former subtitle band.
    """
    started = time.monotonic()
    metrics.update(mode="adaptive", coarse_frames=0, refinement_frames=0,
                   recognized_frames=0, visual_reused=0, sampled_frames=0,
                   decoded_samples=0, grabbed_frames=0, seeks=0,
                   locked_segments=0, refined_segments=0, peak_batch_frames=0,
                   sample_intervals={})
    reader = OrderedFrameReader(cap, fps, width, height, stopped, metrics, duration)
    samples = []

    def run(images, category):
        if stopped():
            raise RuntimeError("OCR cancelled")
        result = recognize(images)
        if len(result) != len(images):
            raise RuntimeError("OCR returned incomplete adaptive results")
        metrics[category] += len(images)
        metrics["recognized_frames"] += len(images)
        metrics["peak_batch_frames"] = max(metrics["peak_batch_frames"], len(images))
        return result

    try:
        ordered = sorted(enumerate(segments), key=lambda pair: pair[1].start)
        for position, (sid, segment) in enumerate(ordered):
            start = max(0., segment.start.total_seconds())
            end = segment.end.total_seconds()
            if duration > 0:
                end = min(end, duration)
            if end <= start:
                continue
            coarse_times = sorted({round(start + (end - start) * fraction, 6)
                                   for fraction in ((.5,) if end - start <= .6 else (.3, .7))})
            coarse = [(t, *reader.read(t)) for t in coarse_times]
            coarse_rows = run([item[1] for item in coarse], "coarse_frames")
            source_id = getattr(segment, "index", sid)
            candidates = []
            for (timestamp, frame, ratio), rows in zip(coarse, coarse_rows):
                for box, text, confidence in rows:
                    if len(box) < 4:
                        continue
                    candidates.append(dict(
                        text=text, prob=confidence, sample_segment_id=source_id,
                        sample_time=timestamp,
                        x_pct=min(p[0] for p in box) / ratio / width,
                        max_x_pct=max(p[0] for p in box) / ratio / width,
                        y_pct=(min(p[1] for p in box) / ratio + int(height * .05)) / height,
                        max_y_pct=(max(p[1] for p in box) / ratio + int(height * .05)) / height,
                    ))
            band = select_chinese_subtitle_band(
                candidates, {source_id: segment.content}, width, height)
            locked = band.support > 0
            if locked:
                metrics["locked_segments"] += 1
            refinement_before = metrics["refinement_frames"]
            # Compare the entire search crop, not only the old band: a second
            # line or a subtitle jumping elsewhere must invalidate the cache.
            cache = SubtitleFrameCache(0., 1., max_age=3.0 if locked and end-start <= 3 else 1.0)
            exact = {}
            for (timestamp, frame, ratio), rows in zip(coarse, coarse_rows):
                holder = {"rows": rows}
                exact[round(timestamp * fps)] = (ratio, holder)
                cache.remember(cache.signature(frame), timestamp, holder)

            left = max(0., start - .3)
            next_start = (ordered[position + 1][1].start.total_seconds()
                          if position + 1 < len(ordered) else duration)
            right = max(end, next_start) + .3
            if duration > 0:
                right = min(right, duration)
            step = .2 if locked and end - start <= 3 else .5
            metrics["sample_intervals"][str(source_id)] = step
            count = max(1, math.ceil((right - left) / step))
            times = sorted({round(left + (n + .5) * (right-left) / count, 6)
                            for n in range(count)} | set(coarse_times)
                           | {round((start + end) / 2, 6)})
            pending = []

            def flush():
                if not pending:
                    return
                result = run([item[0] for item in pending], "refinement_frames")
                for (_, holder), rows in zip(pending, result):
                    holder["rows"] = rows
                pending.clear()

            for timestamp in times:
                if stopped():
                    raise RuntimeError("OCR cancelled")
                frame_key = round(timestamp * fps)
                if frame_key in exact:
                    ratio, holder = exact[frame_key]
                else:
                    frame, ratio = reader.read(timestamp)
                    signature = cache.signature(frame)
                    holder = cache.lookup(signature, timestamp)
                    if holder is not None:
                        metrics["visual_reused"] += 1
                    else:
                        holder = {}
                        cache.remember(signature, timestamp, holder)
                        pending.append((frame, holder))
                        if len(pending) >= 12:
                            flush()
                samples.append((timestamp, ratio, segment, sid, holder))
            flush()
            if not locked or end-start > 3 or metrics["refinement_frames"] > refinement_before:
                metrics["refined_segments"] += 1
            # Drop full-resolution coarse/pending frames before the next cue.
            coarse.clear()
        samples.sort(key=lambda item: item[0])
        metrics["sampled_frames"] = len(samples)
        return samples
    finally:
        cap.release()
        metrics["acquisition_seconds"] = round(time.monotonic() - started, 3)
